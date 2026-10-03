"""轮次计时：状态机、剩余时间推算与幂等命令应用。

计时状态持久化在 round_clocks 单行中（每轮至多一行，由 start 创建）：

- started_at：服务端 UTC 锚点（计时起点）；
- accumulated_pause_seconds：累计暂停时长；
- paused_at / ended_at：暂停中 / 已封盘的冻结点；
- match_extensions：各非轮空对局的累计加时（extend 叠加）。

剩余时间一律在读取/校验时由「锚点 + 累计暂停」推算：

    有效计时终点 = paused_at（暂停中）| ended_at（已封盘）| now（运行中）
    已用时长     = 有效计时终点 - started_at - accumulated_pause_seconds
    剩余秒数     = total_seconds + 该场加时 - 已用时长

不写任何逐秒任务：进程/容器重启后读数由持久化锚点继续推算，自然连续。

注意：apply_clock_action 只在调用方事务内写入并 flush，不提交；
时钟行、幂等记录（clock_actions）与 round_clock.* 事件由路由层
在同一事务中统一提交。被拒绝的命令（非法状态/版本冲突 409）在
任何写入之前抛出，不落库。
"""
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..events.service import (
    ROUND_CLOCK_EXTENDED,
    ROUND_CLOCK_PAUSED,
    ROUND_CLOCK_RESUMED,
    ROUND_CLOCK_STARTED,
    append_event,
)
from ..models import ClockAction, Event, Match, Round, RoundClock
from ..schemas import ClockActionRequest, ClockMatchResponse, ClockResponse


def utcnow() -> datetime:
    """服务端 UTC 当前时间（naive，与库内其他时间戳一致）。

    所有计时读数统一经此函数取时，便于测试注入固定时间。
    """
    return datetime.utcnow()


def get_clock(db: Session, round_id: int) -> RoundClock | None:
    return (
        db.query(RoundClock)
        .filter(RoundClock.round_id == round_id)
        .first()
    )


def _iso_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat() + "Z"


def _effective_end(clock: RoundClock, now: datetime) -> datetime:
    """推算已用时长所用的计时终点：暂停/封盘时冻结，否则为当前时刻。"""
    if clock.status == "paused":
        return clock.paused_at
    if clock.status == "ended":
        return clock.ended_at
    return now


def elapsed_seconds(clock: RoundClock, now: datetime) -> float:
    """扣除累计暂停后的已用计时秒数。"""
    end = _effective_end(clock, now)
    return (
        (end - clock.started_at).total_seconds()
        - clock.accumulated_pause_seconds
    )


def match_timing(
    clock: RoundClock, extra_seconds: int, now: datetime
) -> tuple[datetime | None, int]:
    """由锚点推算某场的截止时刻与剩余秒数。

    暂停中截止时刻未知（取决于何时恢复），返回 None；剩余秒数超时为负。
    """
    deadline: datetime | None = None
    if clock.status != "paused":
        deadline = clock.started_at + timedelta(
            seconds=clock.accumulated_pause_seconds
            + clock.total_seconds
            + extra_seconds
        )
    remaining = (
        clock.total_seconds + extra_seconds - elapsed_seconds(clock, now)
    )
    return deadline, int(round(remaining))


def result_deadline_exceeded(
    clock: RoundClock | None, match_id: int
) -> bool:
    """当前时刻是否已晚于该场截止时刻（暂停中按冻结的剩余时间判定）。

    计时未启动（无时钟行）或轮次已封盘（ended，此时轮次已关闭，
    由轮次状态拦截）时不做截止校验。
    """
    if clock is None or clock.status not in ("running", "paused"):
        return False
    now = utcnow()
    extra = (clock.match_extensions or {}).get(str(match_id), 0)
    return elapsed_seconds(clock, now) > clock.total_seconds + extra


def freeze_clock_for_closed_round(db: Session, round_id: int) -> None:
    """轮次封盘时冻结其时钟（ended）：剩余时间停在封盘时刻。

    暂停中封盘等价于「先在封盘时刻恢复、再立即结束」：把最后一段
    暂停计入累计暂停，保证已用时长推算正确。
    """
    clock = get_clock(db, round_id)
    if clock is None or clock.status not in ("running", "paused"):
        return
    now = utcnow()
    if clock.status == "paused":
        clock.accumulated_pause_seconds += (
            now - clock.paused_at
        ).total_seconds()
        clock.paused_at = None
    clock.tournament_paused = False
    clock.status = "ended"
    clock.ended_at = now
    db.flush()


def freeze_running_clocks_for_tournament(
    db: Session, tournament_id: int, now: datetime
) -> list[int]:
    """赛事进入维护态：冻结该赛事所有正在运行（running）的轮次时钟。

    只冻结 running 的时钟：手动暂停（paused 且 tournament_paused=False）
    的时钟本来就停走，维护期间与恢复后都保持手动暂停；已经封盘
    （ended）与未启动（无行）的时钟不受影响。返回被冻结的轮次号
    （升序）。冻结不推进时钟版本（不产生 round_clock.* 命令事件），
    维护时长经 paused_at 锚点在恢复时补入累计暂停，剩余时间不会因
    维护流逝。
    """
    rows = (
        db.query(RoundClock, Round)
        .join(Round, Round.id == RoundClock.round_id)
        .filter(
            Round.tournament_id == tournament_id,
            RoundClock.status == "running",
        )
        .order_by(Round.round_no)
        .all()
    )
    frozen_rounds: list[int] = []
    for clock, round_obj in rows:
        clock.status = "paused"
        clock.paused_at = now
        clock.tournament_paused = True
        frozen_rounds.append(round_obj.round_no)
    if rows:
        db.flush()
    return frozen_rounds


def resume_tournament_paused_clocks(
    db: Session, tournament_id: int, now: datetime
) -> list[int]:
    """赛事结束维护：只恢复被赛事暂停（tournament_paused=True）的时钟。

    手动暂停（tournament_paused=False）的时钟保持暂停，其
    paused_at 锚点未被维护触碰，维护时长不会计入该时钟的累计暂停。
    返回恢复的轮次号（升序）。
    """
    rows = (
        db.query(RoundClock, Round)
        .join(Round, Round.id == RoundClock.round_id)
        .filter(
            Round.tournament_id == tournament_id,
            RoundClock.status == "paused",
            RoundClock.tournament_paused.is_(True),
        )
        .order_by(Round.round_no)
        .all()
    )
    resumed_rounds: list[int] = []
    for clock, round_obj in rows:
        clock.accumulated_pause_seconds += (
            now - clock.paused_at
        ).total_seconds()
        clock.paused_at = None
        clock.tournament_paused = False
        clock.status = "running"
        resumed_rounds.append(round_obj.round_no)
    if rows:
        db.flush()
    return resumed_rounds


def serialize_clock(
    db: Session,
    tournament_id: int,
    round_obj: Round,
    clock: RoundClock | None,
    *,
    now: datetime | None = None,
) -> ClockResponse:
    """组装时钟响应：版本、状态与各非轮空对局的截止时刻/剩余秒数。"""
    now = now or utcnow()
    matches = (
        db.query(Match)
        .filter(
            Match.round_id == round_obj.id,
            Match.player_black_id.is_not(None),
        )
        .order_by(Match.id)
        .all()
    )
    if clock is None:
        return ClockResponse(
            tournament_id=tournament_id,
            round_id=round_obj.id,
            round_no=round_obj.round_no,
            version=0,
            status="idle",
            total_seconds=None,
            server_time=now,
            matches=[
                ClockMatchResponse(match_id=m.id, extra_seconds=0)
                for m in matches
            ],
        )

    extensions = clock.match_extensions or {}
    match_responses = []
    for m in matches:
        extra = extensions.get(str(m.id), 0)
        deadline, remaining = match_timing(clock, extra, now)
        match_responses.append(
            ClockMatchResponse(
                match_id=m.id,
                extra_seconds=extra,
                deadline_at=deadline,
                remaining_seconds=remaining,
            )
        )
    return ClockResponse(
        tournament_id=tournament_id,
        round_id=round_obj.id,
        round_no=round_obj.round_no,
        version=clock.version,
        status=clock.status,
        total_seconds=clock.total_seconds,
        started_at=clock.started_at,
        paused_at=clock.paused_at,
        ended_at=clock.ended_at,
        accumulated_pause_seconds=clock.accumulated_pause_seconds,
        tournament_paused=bool(clock.tournament_paused),
        server_time=now,
        matches=match_responses,
    )


def _params_fingerprint(req: ClockActionRequest) -> dict:
    """命令参数指纹：幂等重放判定时忽略无关字段。"""
    if req.action == "start":
        return {"total_seconds": req.total_seconds}
    if req.action == "extend":
        return {"match_id": req.match_id, "seconds": req.seconds}
    return {}


def _find_replay(
    db: Session, clock: RoundClock, req: ClockActionRequest
) -> ClockResponse | None:
    """同一 request_id 的已成功命令：参数一致返回首次结果，否则 409。"""
    record = (
        db.query(ClockAction)
        .filter(
            ClockAction.clock_id == clock.id,
            ClockAction.request_id == req.request_id,
        )
        .first()
    )
    if record is None:
        return None
    if (
        record.action == req.action
        and record.expected_version == req.expected_version
        and record.params == _params_fingerprint(req)
    ):
        return ClockResponse(**record.response)
    raise HTTPException(
        status_code=409,
        detail="request_id 已被用于其他计时命令，请更换 request_id",
    )


def apply_clock_action(
    db: Session,
    tournament_id: int,
    round_obj: Round,
    req: ClockActionRequest,
) -> tuple[ClockResponse, Event | None]:
    """校验并应用一个计时命令，返回（响应, 事件）。

    幂等重放时返回（首次响应, None），不产生任何写入；
    非法状态/版本冲突抛 409，且在任何写入之前抛出（不落库）。
    调用方负责在同一事务中提交响应涉及的写入与事件。
    """
    clock = get_clock(db, round_obj.id)

    # 幂等判定先于状态/版本校验：重试已成功命令必须返回原结果，
    # 而不是因版本已推进报 409
    if clock is not None:
        replay = _find_replay(db, clock, req)
        if replay is not None:
            return replay, None

    # 维护/归档期间计时命令不允许产生新写入（已成功命令的重放
    # 已在上方返回首次结果）；paused 下的手动 resume 也在此拦截，
    # 运行中时钟在 pause 时已被赛事冻结
    from .lifecycle import ensure_tournament_writable

    ensure_tournament_writable(db, tournament_id)

    if round_obj.status != "open":
        raise HTTPException(
            status_code=409, detail="该轮次已结束，不能再执行计时命令"
        )

    current_version = clock.version if clock is not None else 0
    if req.expected_version != current_version:
        raise HTTPException(
            status_code=409,
            detail=f"时钟版本冲突：当前版本 {current_version}，"
            f"请求期望 {req.expected_version}",
        )

    now = utcnow()
    if req.action == "start":
        clock, event_type, payload = _apply_start(
            db, clock, round_obj, req, now
        )
    elif req.action == "pause":
        clock, event_type, payload = _apply_pause(clock, round_obj, now)
    elif req.action == "resume":
        clock, event_type, payload = _apply_resume(clock, round_obj, now)
    else:  # extend
        clock, event_type, payload = _apply_extend(db, clock, round_obj, req)

    clock.version += 1
    payload["version"] = clock.version
    db.flush()

    response = serialize_clock(db, tournament_id, round_obj, clock, now=now)
    db.add(
        ClockAction(
            clock_id=clock.id,
            request_id=req.request_id,
            action=req.action,
            expected_version=req.expected_version,
            params=_params_fingerprint(req),
            response=response.model_dump(mode="json"),
        )
    )
    event = append_event(db, tournament_id, event_type, payload)
    return response, event


def _require_clock(clock: RoundClock | None, action: str) -> RoundClock:
    if clock is None:
        raise HTTPException(
            status_code=409, detail=f"计时尚未启动，不能执行 {action}"
        )
    return clock


def _apply_start(
    db: Session,
    clock: RoundClock | None,
    round_obj: Round,
    req: ClockActionRequest,
    now: datetime,
) -> tuple[RoundClock, str, dict]:
    if clock is not None:
        raise HTTPException(status_code=409, detail="计时已启动，不能重复 start")
    clock = RoundClock(
        round_id=round_obj.id,
        version=0,
        status="running",
        total_seconds=req.total_seconds,
        started_at=now,
        accumulated_pause_seconds=0.0,
        paused_at=None,
        tournament_paused=False,
        ended_at=None,
        match_extensions={},
    )
    db.add(clock)
    db.flush()  # 取得 clock.id，供幂等记录使用
    return clock, ROUND_CLOCK_STARTED, {
        "round_id": round_obj.id,
        "round_no": round_obj.round_no,
        "total_seconds": req.total_seconds,
        "started_at": _iso_z(now),
    }


def _apply_pause(
    clock: RoundClock | None, round_obj: Round, now: datetime
) -> tuple[RoundClock, str, dict]:
    clock = _require_clock(clock, "pause")
    if clock.status != "running":
        raise HTTPException(
            status_code=409, detail="计时未在运行，不能暂停"
        )
    clock.paused_at = now
    clock.status = "paused"
    # 手动暂停：不是赛事维护冻结
    clock.tournament_paused = False
    return clock, ROUND_CLOCK_PAUSED, {
        "round_id": round_obj.id,
        "round_no": round_obj.round_no,
        "paused_at": _iso_z(now),
    }


def _apply_resume(
    clock: RoundClock | None, round_obj: Round, now: datetime
) -> tuple[RoundClock, str, dict]:
    clock = _require_clock(clock, "resume")
    if clock.status != "paused":
        raise HTTPException(
            status_code=409, detail="计时未处于暂停状态，不能恢复"
        )
    if clock.tournament_paused:
        raise HTTPException(
            status_code=409,
            detail="该时钟由赛事维护冻结，只能随赛事 resume 恢复，"
            "不接受手动 resume",
        )
    clock.accumulated_pause_seconds += (now - clock.paused_at).total_seconds()
    clock.paused_at = None
    clock.status = "running"
    return clock, ROUND_CLOCK_RESUMED, {
        "round_id": round_obj.id,
        "round_no": round_obj.round_no,
        "accumulated_pause_seconds": clock.accumulated_pause_seconds,
    }


def _apply_extend(
    db: Session,
    clock: RoundClock | None,
    round_obj: Round,
    req: ClockActionRequest,
) -> tuple[RoundClock, str, dict]:
    clock = _require_clock(clock, "extend")
    if clock.status not in ("running", "paused"):
        raise HTTPException(
            status_code=409, detail="计时已结束，不能加时"
        )
    if clock.tournament_paused:
        raise HTTPException(
            status_code=409, detail="赛事维护期间不能加时"
        )
    match = db.get(Match, req.match_id)
    if match is None or match.round_id != round_obj.id:
        raise HTTPException(status_code=404, detail="该轮次中不存在此对局")
    if match.player_black_id is None:
        raise HTTPException(status_code=409, detail="轮空对局无需加时")
    if match.score_white is not None:
        raise HTTPException(
            status_code=409, detail="该对局已提交赛果，不能加时"
        )
    extensions = dict(clock.match_extensions or {})
    key = str(match.id)
    extensions[key] = extensions.get(key, 0) + req.seconds
    clock.match_extensions = extensions
    return clock, ROUND_CLOCK_EXTENDED, {
        "round_id": round_obj.id,
        "round_no": round_obj.round_no,
        "match_id": match.id,
        "seconds": req.seconds,
        "extra_seconds": extensions[key],
    }
