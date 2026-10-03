"""赛事生命周期状态控制：active / paused（维护态）/ archived（归档）。

- 赛事创建即为 active；运营方携带 request_id 幂等键与
  expected_event_seq 乐观快照执行 pause / resume / archive：
  * pause：active -> paused。同一事务内把所有运行中的轮次时钟冻结
    为暂停（置 paused_by_maintenance 标记），维护期间剩余时间不再
    流逝；
  * resume：paused -> active。同一事务内只恢复被赛事暂停的时钟
    （paused_by_maintenance=True），手动暂停的时钟保持暂停；维护
    窗口计入累计暂停时长，剩余时间不因维护流逝；
  * archive：paused -> archived。仅允许在所有已生成轮次均结束且
    没有待处理赛果复核时进入；归档后永久只读，不可再变更。
- 每次成功切换与 tournament.paused / tournament.resumed /
  tournament.archived 事件在同一事务中提交；重复 request_id（参数
  一致）原样返回首次结果，同键不同参数返回 409。
- 维护态与归档赛事下，参赛者、轮次、赛果、签到、工位、计时、公告、
  复核、扣分、晋级等写操作统一返回 409 且不落任何记录（查询与事件
  续传不受影响）；守卫即本模块的 ensure_tournament_writable。

注意：apply_status_action 只在调用方事务内写入并 flush，不提交；
状态变更、时钟冻结/恢复、幂等记录与 tournament.* 事件由路由层在
同一事务中统一提交。所有调用均处于进程级 writer_lock 内。
"""
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..events.service import (
    TOURNAMENT_ARCHIVED,
    TOURNAMENT_PAUSED,
    TOURNAMENT_RESUMED,
    append_event,
)
from ..models import Event, Round, RoundClock, Tournament, TournamentStateAction
from ..schemas import (
    StatusClockBrief,
    TournamentStatusActionRequest,
    TournamentStatusResponse,
)
from . import clock as clock_service
from .announcements import current_event_seq

# 赛事状态
ACTIVE = "active"
PAUSED = "paused"
ARCHIVED = "archived"

# 状态切换命令
PAUSE = "pause"
RESUME = "resume"
ARCHIVE = "archive"


def tournament_status(db: Session, tournament_id: int) -> str | None:
    """读取赛事当前状态（标量查询绕过会话缓存，锁内读取始终最新）。"""
    return db.execute(
        select(Tournament.status).where(Tournament.id == tournament_id)
    ).scalar_one_or_none()


def ensure_tournament_writable(db: Session, tournament_id: int) -> None:
    """写操作守卫：维护态/归档赛事一律 409，且在任何写入之前抛出。

    所有赛事级写请求在 writer_lock 内、业务校验与幂等判定之前调用；
    被拒绝的请求不落任何记录、不占事件序号。
    """
    status = tournament_status(db, tournament_id)
    if status is None:
        raise HTTPException(status_code=404, detail="赛事不存在")
    if status == PAUSED:
        raise HTTPException(
            status_code=409,
            detail="赛事处于维护态（paused），写操作暂不可用，待恢复后重试",
        )
    if status == ARCHIVED:
        raise HTTPException(
            status_code=409,
            detail="赛事已归档（archived），永久只读，不能再执行写操作",
        )


def _params_fingerprint(req: TournamentStatusActionRequest) -> dict:
    return {
        "action": req.action,
        "expected_event_seq": req.expected_event_seq,
    }


def _find_replay(
    db: Session, tournament_id: int, req: TournamentStatusActionRequest
) -> TournamentStatusResponse | None:
    """同一 request_id 的已成功切换：参数一致返回首次结果，否则 409。"""
    record = (
        db.query(TournamentStateAction)
        .filter(
            TournamentStateAction.tournament_id == tournament_id,
            TournamentStateAction.request_id == req.request_id,
        )
        .first()
    )
    if record is None:
        return None
    if record.action == req.action and record.params == _params_fingerprint(req):
        return TournamentStatusResponse(**record.response)
    raise HTTPException(
        status_code=409,
        detail="request_id 已被用于该赛事的其他状态切换请求，请更换 request_id",
    )


def _freeze_running_clocks(
    db: Session, tournament_id: int, now: datetime
) -> list[StatusClockBrief]:
    """把本赛事所有运行中的轮次时钟冻结为维护态暂停（同事务）。

    已暂停（手动）与已封盘的时钟不动；被冻结的时钟置
    paused_by_maintenance 标记，供赛事 resume 时选择性恢复。
    """
    clocks = (
        db.query(RoundClock)
        .join(Round, Round.id == RoundClock.round_id)
        .filter(
            Round.tournament_id == tournament_id,
            RoundClock.status == "running",
        )
        .order_by(Round.round_no)
        .all()
    )
    frozen = []
    for clock in clocks:
        clock.status = "paused"
        clock.paused_at = now
        clock.paused_by_maintenance = True
        round_obj = db.get(Round, clock.round_id)
        frozen.append(
            StatusClockBrief(round_id=clock.round_id, round_no=round_obj.round_no)
        )
    if frozen:
        db.flush()
    return frozen


def _resume_maintenance_clocks(
    db: Session, tournament_id: int, now: datetime
) -> list[StatusClockBrief]:
    """恢复被赛事维护态暂停的时钟（同事务）；手动暂停的保持暂停。

    维护窗口计入累计暂停时长：剩余时间在维护期间不流逝。
    """
    clocks = (
        db.query(RoundClock)
        .join(Round, Round.id == RoundClock.round_id)
        .filter(
            Round.tournament_id == tournament_id,
            RoundClock.status == "paused",
            RoundClock.paused_by_maintenance.is_(True),
        )
        .order_by(Round.round_no)
        .all()
    )
    resumed = []
    for clock in clocks:
        clock.accumulated_pause_seconds += (now - clock.paused_at).total_seconds()
        clock.paused_at = None
        clock.paused_by_maintenance = False
        clock.status = "running"
        round_obj = db.get(Round, clock.round_id)
        resumed.append(
            StatusClockBrief(round_id=clock.round_id, round_no=round_obj.round_no)
        )
    if resumed:
        db.flush()
    return resumed


def _ensure_archivable(db: Session, tournament_id: int) -> None:
    """归档前置条件：所有已生成轮次均已结束，且没有待处理赛果复核。"""
    # 延迟导入：result_reviews 的写守卫依赖本模块，避免循环导入
    from .result_reviews import has_pending_review_for_tournament

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


def apply_status_action(
    db: Session, tournament_id: int, req: TournamentStatusActionRequest
) -> tuple[TournamentStatusResponse, Event | None]:
    """校验并应用一次赛事状态切换，返回（响应, 事件）。

    幂等重放时返回（首次响应, None），不产生任何写入；非法状态迁移、
    归档前置条件不满足或事件序号快照冲突抛 409，且在任何写入之前
    抛出（不落库）。调用方负责在同一事务中提交响应涉及的写入与事件。
    """
    # 幂等判定先于状态机校验：重试已成功请求必须返回首次结果
    replay = _find_replay(db, tournament_id, req)
    if replay is not None:
        return replay, None

    # 锁内以最新提交状态校验（标量查询绕过会话缓存）
    current = tournament_status(db, tournament_id)
    if current is None:
        raise HTTPException(status_code=404, detail="赛事不存在")

    if req.action == PAUSE:
        if current != ACTIVE:
            raise HTTPException(
                status_code=409,
                detail=f"赛事当前状态为 {current}，只有 active 才能暂停为维护态",
            )
    elif req.action == RESUME:
        if current != PAUSED:
            raise HTTPException(
                status_code=409,
                detail=f"赛事当前状态为 {current}，只有 paused 才能恢复",
            )
    else:  # archive
        if current != PAUSED:
            raise HTTPException(
                status_code=409,
                detail=f"赛事当前状态为 {current}，只有 paused 才能归档",
            )
        _ensure_archivable(db, tournament_id)

    # 先完成全部状态机校验，再在同一写事务内确认事件序号快照未变化
    actual_seq = current_event_seq(db, tournament_id)
    if req.expected_event_seq != actual_seq:
        raise HTTPException(
            status_code=409,
            detail=(
                f"赛事快照已变化：当前最大事件序号为 {actual_seq}，"
                f"请求期望 {req.expected_event_seq}"
            ),
        )

    tournament = db.get(Tournament, tournament_id)
    now = clock_service.utcnow()
    frozen: list[StatusClockBrief] = []
    resumed: list[StatusClockBrief] = []
    if req.action == PAUSE:
        tournament.status = PAUSED
        # 同一事务内冻结所有运行中的轮次时钟
        frozen = _freeze_running_clocks(db, tournament_id, now)
        new_status, event_type = PAUSED, TOURNAMENT_PAUSED
    elif req.action == RESUME:
        tournament.status = ACTIVE
        # 只恢复被赛事暂停的时钟；手动暂停的保持暂停
        resumed = _resume_maintenance_clocks(db, tournament_id, now)
        new_status, event_type = ACTIVE, TOURNAMENT_RESUMED
    else:
        tournament.status = ARCHIVED
        new_status, event_type = ARCHIVED, TOURNAMENT_ARCHIVED
    db.flush()

    event = append_event(
        db,
        tournament_id,
        event_type,
        {
            "previous_status": current,
            "status": new_status,
            "frozen_clocks": [c.model_dump() for c in frozen],
            "resumed_clocks": [c.model_dump() for c in resumed],
            "expected_event_seq": req.expected_event_seq,
        },
    )

    response = TournamentStatusResponse(
        tournament_id=tournament_id,
        status=new_status,
        previous_status=current,
        action=req.action,
        request_id=req.request_id,
        expected_event_seq=req.expected_event_seq,
        event_seq=event.seq,
        frozen_clocks=frozen,
        resumed_clocks=resumed,
        changed_at=now,
    )
    db.add(
        TournamentStateAction(
            tournament_id=tournament_id,
            request_id=req.request_id,
            action=req.action,
            params=_params_fingerprint(req),
            response=response.model_dump(mode="json"),
        )
    )
    db.flush()
    return response, event
