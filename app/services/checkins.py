"""现场签到快照：在目标轮次生成前维护到场/缺席名单。

签到快照只面向“尚未生成的下一轮”。提交时两组参赛者编号必须恰好
覆盖当前报名名单且互不相交；重复 request_id 在参数一致时原样返回
首次结果。目标轮次生成后快照冻结，不能再变更或确认。

成功提交名单会写入 checkin.snapshot_changed，首次确认（或名单变更
后的再次确认）写入 checkin.snapshot_confirmed；事件与快照、幂等
动作记录在同一个调用方事务中提交。
"""
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..events.service import (
    CHECKIN_SNAPSHOT_CHANGED,
    CHECKIN_SNAPSHOT_CONFIRMED,
    append_event,
)
from ..models import CheckInAction, CheckInSnapshot, Event, Participant, Round
from ..schemas import (
    CheckInConfirmRequest,
    CheckInParticipantResponse,
    CheckInSnapshotRequest,
    CheckInSnapshotResponse,
)
from .announcements import current_event_seq

SUBMIT = "submit"
CONFIRM = "confirm"


def _participants_by_number(
    db: Session, tournament_id: int
) -> dict[int, Participant]:
    rows = (
        db.query(Participant)
        .filter(Participant.tournament_id == tournament_id)
        .order_by(Participant.number)
        .all()
    )
    return {p.number: p for p in rows}


def _next_round_no(db: Session, tournament_id: int) -> int:
    last = (
        db.query(Round)
        .filter(Round.tournament_id == tournament_id)
        .order_by(Round.round_no.desc())
        .first()
    )
    return (last.round_no + 1) if last is not None else 1


def _ensure_target_is_ungenerated_next(
    db: Session, tournament_id: int, round_no: int
) -> None:
    existing = (
        db.query(Round)
        .filter(
            Round.tournament_id == tournament_id,
            Round.round_no == round_no,
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409, detail=f"第 {round_no} 轮已经生成，签到快照已冻结"
        )

    next_no = _next_round_no(db, tournament_id)
    if round_no != next_no:
        raise HTTPException(
            status_code=409,
            detail=f"当前只能为尚未生成的下一轮（第 {next_no} 轮）建立签到快照",
        )


def _check_event_seq(db: Session, tournament_id: int, expected: int) -> None:
    actual = current_event_seq(db, tournament_id)
    if actual != expected:
        raise HTTPException(
            status_code=409,
            detail=(
                f"赛事快照已变化：当前最大事件序号为 {actual}，请求期望 {expected}"
            ),
        )


def _find_replay(
    db: Session,
    tournament_id: int,
    request_id: str,
    action: str,
    fingerprint: dict,
) -> CheckInSnapshotResponse | None:
    row = (
        db.query(CheckInAction)
        .filter(
            CheckInAction.tournament_id == tournament_id,
            CheckInAction.request_id == request_id,
        )
        .first()
    )
    if row is None:
        return None
    if row.action != action or row.params != fingerprint:
        raise HTTPException(
            status_code=409,
            detail="request_id 已被用于该赛事的其他签到请求，请更换 request_id",
        )
    return CheckInSnapshotResponse(**row.response)


def serialize_snapshot(snapshot: CheckInSnapshot, db: Session) -> CheckInSnapshotResponse:
    participants = _participants_by_number(db, snapshot.tournament_id)
    present = set(snapshot.present_numbers)
    absent = set(snapshot.absent_numbers)
    roster = [
        CheckInParticipantResponse(
            participant_id=p.id,
            number=p.number,
            name=p.name,
            attended=p.number in present,
        )
        for p in participants.values()
        if p.number in present or p.number in absent
    ]
    return CheckInSnapshotResponse(
        id=snapshot.id,
        tournament_id=snapshot.tournament_id,
        round_no=snapshot.round_no,
        version=snapshot.version,
        present_numbers=list(snapshot.present_numbers),
        absent_numbers=list(snapshot.absent_numbers),
        roster=roster,
        confirmed=snapshot.confirmed,
        consumed=snapshot.consumed,
        confirmed_at=snapshot.confirmed_at,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
    )


def _save_action(
    db: Session,
    snapshot: CheckInSnapshot,
    request_id: str,
    action: str,
    params: dict,
    response: CheckInSnapshotResponse,
) -> None:
    db.add(
        CheckInAction(
            tournament_id=snapshot.tournament_id,
            snapshot_id=snapshot.id,
            request_id=request_id,
            action=action,
            params=params,
            response=response.model_dump(mode="json"),
        )
    )


def submit_snapshot(
    db: Session, tournament_id: int, req: CheckInSnapshotRequest
) -> tuple[CheckInSnapshotResponse, Event | None]:
    """创建或更新下一轮签到快照（flush，不提交）。"""
    present_sorted = sorted(req.present_numbers)
    absent_sorted = sorted(req.absent_numbers)
    fingerprint = {
        "action": SUBMIT,
        "round_no": req.round_no,
        "expected_event_seq": req.expected_event_seq,
        "present_numbers": present_sorted,
        "absent_numbers": absent_sorted,
    }
    # 幂等判定先于业务校验：已成功请求的网络重试必须返回首次结果。
    replay = _find_replay(
        db, tournament_id, req.request_id, SUBMIT, fingerprint
    )
    if replay is not None:
        return replay, None

    # 维护/归档期间拒绝新的签到写入（已成功请求的重放上方已返回）
    from .lifecycle import ensure_tournament_writable

    ensure_tournament_writable(db, tournament_id)

    participants = _participants_by_number(db, tournament_id)
    if not participants:
        raise HTTPException(status_code=400, detail="赛事还没有参赛者，无法签到")

    if len(present_sorted) != len(set(present_sorted)):
        raise HTTPException(status_code=409, detail="到场名单中存在重复参赛者编号")
    if len(absent_sorted) != len(set(absent_sorted)):
        raise HTTPException(status_code=409, detail="缺席名单中存在重复参赛者编号")
    if set(present_sorted).intersection(absent_sorted):
        raise HTTPException(status_code=409, detail="同一参赛者不能同时到场和缺席")

    registered = set(participants)
    supplied = set(present_sorted).union(absent_sorted)
    if supplied != registered:
        missing = sorted(registered - supplied)
        unknown = sorted(supplied - registered)
        detail = "到场/缺席名单合并后必须恰好覆盖当前报名名单"
        if missing:
            detail += f"；缺少编号：{missing}"
        if unknown:
            detail += f"；未知编号：{unknown}"
        raise HTTPException(status_code=409, detail=detail)

    _ensure_target_is_ungenerated_next(
        db, tournament_id, req.round_no
    )
    _check_event_seq(db, tournament_id, req.expected_event_seq)

    snapshot = (
        db.query(CheckInSnapshot)
        .filter(
            CheckInSnapshot.tournament_id == tournament_id,
            CheckInSnapshot.round_no == req.round_no,
        )
        .first()
    )
    now = datetime.utcnow()
    created = snapshot is None
    if snapshot is None:
        snapshot = CheckInSnapshot(
            tournament_id=tournament_id,
            round_no=req.round_no,
            version=1,
            present_numbers=present_sorted,
            absent_numbers=absent_sorted,
            confirmed=False,
            consumed=False,
            created_at=now,
            updated_at=now,
        )
        db.add(snapshot)
    else:
        snapshot.version += 1
        snapshot.present_numbers = present_sorted
        snapshot.absent_numbers = absent_sorted
        # 名单变化后旧确认不再沿用，需重新确认
        snapshot.confirmed = False
        snapshot.confirmed_at = None
        snapshot.updated_at = now
    db.flush()

    event = append_event(
        db,
        tournament_id,
        CHECKIN_SNAPSHOT_CHANGED,
        {
            "snapshot_id": snapshot.id,
            "round_no": snapshot.round_no,
            "version": snapshot.version,
            "present_numbers": present_sorted,
            "absent_numbers": absent_sorted,
            "expected_event_seq": req.expected_event_seq,
            "created": created,
        },
    )
    response = serialize_snapshot(snapshot, db)
    _save_action(
        db, snapshot, req.request_id, SUBMIT, fingerprint, response
    )
    db.flush()
    return response, event


def confirm_snapshot(
    db: Session, tournament_id: int, req: CheckInConfirmRequest
) -> tuple[CheckInSnapshotResponse, Event | None]:
    """确认下一轮签到快照（flush，不提交）。"""
    fingerprint = {
        "action": CONFIRM,
        "round_no": req.round_no,
        "expected_event_seq": req.expected_event_seq,
    }
    replay = _find_replay(
        db, tournament_id, req.request_id, CONFIRM, fingerprint
    )
    if replay is not None:
        return replay, None

    # 维护/归档期间拒绝新的确认写入（已成功请求的重放上方已返回）
    from .lifecycle import ensure_tournament_writable

    ensure_tournament_writable(db, tournament_id)

    snapshot = (
        db.query(CheckInSnapshot)
        .filter(
            CheckInSnapshot.tournament_id == tournament_id,
            CheckInSnapshot.round_no == req.round_no,
        )
        .first()
    )
    _ensure_target_is_ungenerated_next(db, tournament_id, req.round_no)
    _check_event_seq(db, tournament_id, req.expected_event_seq)
    if snapshot is None:
        raise HTTPException(status_code=409, detail="该轮次尚无签到快照，不能确认")

    # 不同幂等键的重复确认是无害的状态重放：不再产生确认事件。
    if snapshot.confirmed:
        return serialize_snapshot(snapshot, db), None

    snapshot.confirmed = True
    snapshot.confirmed_at = datetime.utcnow()
    snapshot.updated_at = snapshot.confirmed_at
    db.flush()

    event = append_event(
        db,
        tournament_id,
        CHECKIN_SNAPSHOT_CONFIRMED,
        {
            "snapshot_id": snapshot.id,
            "round_no": snapshot.round_no,
            "version": snapshot.version,
            "present_numbers": list(snapshot.present_numbers),
            "absent_numbers": list(snapshot.absent_numbers),
            "expected_event_seq": req.expected_event_seq,
        },
    )
    response = serialize_snapshot(snapshot, db)
    _save_action(
        db, snapshot, req.request_id, CONFIRM, fingerprint, response
    )
    db.flush()
    return response, event


def get_snapshot(
    db: Session, tournament_id: int, round_no: int
) -> CheckInSnapshot | None:
    return (
        db.query(CheckInSnapshot)
        .filter(
            CheckInSnapshot.tournament_id == tournament_id,
            CheckInSnapshot.round_no == round_no,
        )
        .first()
    )


def list_snapshots(db: Session, tournament_id: int) -> list[CheckInSnapshot]:
    return list(
        db.query(CheckInSnapshot)
        .filter(CheckInSnapshot.tournament_id == tournament_id)
        .order_by(CheckInSnapshot.round_no)
        .all()
    )


def active_snapshot_for_next_round(
    db: Session, tournament_id: int, round_no: int
) -> CheckInSnapshot | None:
    """读取生成轮次要使用的快照；无快照时返回 None（沿用全员编排）。"""
    snapshot = get_snapshot(db, tournament_id, round_no)
    if snapshot is None:
        return None
    if snapshot.consumed:
        raise HTTPException(status_code=409, detail="该签到快照已用于生成轮次")
    registered = {
        p.number
        for p in _participants_by_number(db, tournament_id).values()
    }
    present = set(snapshot.present_numbers)
    absent = set(snapshot.absent_numbers)
    if (
        present.isdisjoint(absent) is False
        or present.union(absent) != registered
    ):
        raise HTTPException(
            status_code=409,
            detail="签到快照与当前报名名单不一致，请更新快照后再生成轮次",
        )
    return snapshot
