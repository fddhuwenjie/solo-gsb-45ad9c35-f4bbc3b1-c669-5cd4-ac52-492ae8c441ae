"""赛事状态机：active / paused / archived 的幂等切换与写保护。

状态转换：

- pause：active -> paused。同一事务内冻结该赛事所有正在运行
  （running）的轮次时钟；手动暂停的时钟保持暂停，维护期间剩余
  时间不会流逝。
- resume：paused -> active。只恢复被赛事暂停冻结的时钟，手动
  暂停的时钟保持暂停；维护时长经 paused_at 锚点补入被恢复时钟的
  累计暂停，剩余时间不因维护流逝。
- archive：paused -> archived（终态）。仅当所有已生成轮次均已
  结束且不存在待处理赛果复核时允许；归档后永久只读。

每次成功切换与 tournament.* 事件在同一事务中提交，并占用一个
连续事件序号；重复 request_id（action/expected_event_seq 一致）
原样返回首次结果，非法转换、快照冲突、归档前置不满足均 409 且
不落任何记录。

维护（paused）与归档（archived）期间，除查询、事件续传与本状态
控制接口（paused 下 resume/archive；archived 下全部拒绝）外，
赛事的一切写操作统一 409 且不留痕。所有写路径都在 writer_lock
内、幂等重放判定之后调用 ensure_tournament_writable()，因此已
成功请求的网络重试仍按各自 request_id 返回首次结果。
"""
from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..events.service import (
    TOURNAMENT_ARCHIVED,
    TOURNAMENT_PAUSED,
    TOURNAMENT_RESUMED,
    append_event,
)
from ..models import Event, Round, Tournament, TournamentLifecycleAction
from ..schemas import TournamentStatusControlRequest, TournamentStatusResponse
from . import clock as clock_service
from .announcements import current_event_seq
from .result_reviews import has_pending_review_for_tournament

ACTIVE = "active"
PAUSED = "paused"
ARCHIVED = "archived"

PAUSE = "pause"
RESUME = "resume"
ARCHIVE = "archive"

# 合法状态转换
_TRANSITIONS: dict[tuple[str, str], str] = {
    (ACTIVE, PAUSE): PAUSED,
    (PAUSED, RESUME): ACTIVE,
    (PAUSED, ARCHIVE): ARCHIVED,
}


def ensure_tournament_writable(db: Session, tournament_id: int) -> None:
    """维护/归档期间拦截一切业务写操作（409，不留痕）。

    必须在 writer_lock 内、各写服务的幂等重放判定之后调用。
    """
    tournament = db.get(Tournament, tournament_id)
    if tournament is None:
        # 资源存在性由各路由/服务自行给出 404；这里只负责状态保护
        return
    if tournament.status == PAUSED:
        raise HTTPException(
            status_code=409,
            detail="赛事处于维护暂停状态，写操作暂不可用；查询与事件"
            "续传不受影响，可等待运营方恢复后重试",
        )
    if tournament.status == ARCHIVED:
        raise HTTPException(
            status_code=409, detail="赛事已归档（永久只读），不能再进行写操作"
        )


def _fingerprint(req: TournamentStatusControlRequest) -> dict:
    return {
        "action": req.action,
        "expected_event_seq": req.expected_event_seq,
    }


def _find_replay(
    db: Session, tournament_id: int, req: TournamentStatusControlRequest
) -> TournamentStatusResponse | None:
    """同 request_id 的已成功请求：参数一致返回首次结果，否则 409。"""
    record = (
        db.query(TournamentLifecycleAction)
        .filter(
            TournamentLifecycleAction.tournament_id == tournament_id,
            TournamentLifecycleAction.request_id == req.request_id,
        )
        .first()
    )
    if record is None:
        return None
    if record.params == _fingerprint(req):
        return TournamentStatusResponse(**record.response)
    raise HTTPException(
        status_code=409,
        detail="request_id 已被用于该赛事的其他状态切换请求，请更换 request_id",
    )


def _ensure_archivable(db: Session, tournament_id: int) -> None:
    """归档前置条件：全部已生成轮次结束且无待处理复核。"""
    open_round = (
        db.query(Round)
        .filter(
            Round.tournament_id == tournament_id,
            Round.status == "open",
        )
        .first()
    )
    if open_round is not None:
        raise HTTPException(
            status_code=409,
            detail=f"第 {open_round.round_no} 轮尚未结束，不能归档赛事",
        )
    if has_pending_review_for_tournament(db, tournament_id):
        raise HTTPException(
            status_code=409,
            detail="赛事存在待处理的赛果复核，需待复核关闭后才能归档",
        )


def apply_tournament_status(
    db: Session, tournament: Tournament, req: TournamentStatusControlRequest
) -> tuple[TournamentStatusResponse, Event]:
    """校验并应用一次赛事状态切换（flush，不提交）。

    幂等重放时返回（首次响应, None 事件）由调用方识别；本函数在
    非法转换/快照冲突/归档前置不满足时抛 409，且在任何写入之前
    抛出（不落库、不占事件序号）。
    """
    tournament_id = tournament.id
    # 幂等判定先于状态/快照校验：重试已成功请求必须返回原结果，
    # 而不是因状态已推进报 409。归档后的重放同样返回首次结果。
    replay = _find_replay(db, tournament_id, req)
    if replay is not None:
        return replay, None  # type: ignore[return-value]

    target = _TRANSITIONS.get((tournament.status, req.action))
    if target is None:
        raise HTTPException(
            status_code=409,
            detail=f"赛事当前为 {tournament.status} 状态，不能执行 {req.action}",
        )

    # 先完成状态转换/归档前置校验，再在同一写事务内确认事件序号
    # 快照未变化（与其他写模块一致：快照冲突不占号、不留痕）
    if req.action == ARCHIVE:
        _ensure_archivable(db, tournament_id)

    actual_seq = current_event_seq(db, tournament_id)
    if req.expected_event_seq != actual_seq:
        raise HTTPException(
            status_code=409,
            detail=(
                f"赛事快照已变化：当前最大事件序号为 {actual_seq}，"
                f"请求期望 {req.expected_event_seq}"
            ),
        )

    now = clock_service.utcnow()
    frozen_rounds: list[int] = []
    resumed_rounds: list[int] = []
    if req.action == PAUSE:
        frozen_rounds = clock_service.freeze_running_clocks_for_tournament(
            db, tournament_id, now
        )
        event_type, payload = TOURNAMENT_PAUSED, {
            "frozen_rounds": frozen_rounds,
        }
    elif req.action == RESUME:
        resumed_rounds = clock_service.resume_tournament_paused_clocks(
            db, tournament_id, now
        )
        event_type, payload = TOURNAMENT_RESUMED, {
            "resumed_rounds": resumed_rounds,
        }
    else:  # archive
        event_type, payload = TOURNAMENT_ARCHIVED, {}

    tournament.status = target
    db.flush()
    event = append_event(db, tournament_id, event_type, payload)

    response = TournamentStatusResponse(
        id=tournament.id,
        name=tournament.name,
        status=tournament.status,
        expected_event_seq=req.expected_event_seq,
        current_event_seq=event.seq,
        frozen_rounds=frozen_rounds,
        resumed_rounds=resumed_rounds,
    )
    db.add(
        TournamentLifecycleAction(
            tournament_id=tournament_id,
            request_id=req.request_id,
            action=req.action,
            expected_event_seq=req.expected_event_seq,
            params=_fingerprint(req),
            response=response.model_dump(mode="json"),
        )
    )
    db.flush()
    return response, event
