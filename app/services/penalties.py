"""赛务争议扣分账本：可追溯的扣分登记与撤销。

- record_penalty：为参赛者登记一笔扣分（deduct 条目）。扣分量须为
  半分单位的正整数（0.5 的整数倍），携带 request_id 幂等键与
  expected_event_seq 乐观快照；仅允许在「所有已生成轮次均已结束
  且没有待处理赛果复核」时登记。成功时在同一写事务内写入
  penalty.recorded 事件。
- revoke_penalty：以一条新的 revoke 条目撤销一笔仍生效的扣分，
  原扣分条目保留并置为 revoked（同一事务内更新），同时写入
  penalty.revoked 事件；重复撤销（目标已 revoked 或目标不是扣分
  条目）拒绝且不落库。
- 幂等：request_id 在赛事内唯一。重复请求参数一致时原样返回首次
  响应快照（不产生任何写入与事件）；同键不同参数返回 409。
- 排名口径：对局积分（points）保留不变；排名积分 = 对局积分 -
  有效扣分，允许为负；排序、Buchholz 与下一轮积分分组均以排名
  积分为准（见 services/scoring.py 与 services/rounds.py）。

注意：本模块的写函数只在调用方事务内写入并 flush，不提交；账本
条目、被撤销条目的状态更新与 penalty.* 事件由路由层在同一事务
中统一提交。所有调用均处于进程级 writer_lock 内，并由
(tournament_id, request_id) 唯一约束与 revokes_entry_id 部分
唯一索引在数据库层兜底。
"""
from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..events.service import (
    PENALTY_RECORDED,
    PENALTY_REVOKED,
    append_event,
)
from ..models import Event, Participant, PenaltyEntry, Round
from ..schemas import (
    ParticipantPenaltyLedgerResponse,
    PenaltyDeductRequest,
    PenaltyEntryResponse,
    PenaltyRevokeRequest,
)
from .announcements import current_event_seq
from .result_reviews import has_pending_review_for_tournament
from .scoring import half_to_float

DEDUCT = "deduct"
REVOKE = "revoke"
ACTIVE = "active"
REVOKED = "revoked"
APPLIED = "applied"


def effective_penalties(db: Session, tournament_id: int) -> dict[int, int]:
    """赛事内各参赛者的当前有效扣分（半分单位）。"""
    rows = (
        db.query(PenaltyEntry.participant_id, func.sum(PenaltyEntry.points))
        .filter(
            PenaltyEntry.tournament_id == tournament_id,
            PenaltyEntry.kind == DEDUCT,
            PenaltyEntry.status == ACTIVE,
        )
        .group_by(PenaltyEntry.participant_id)
        .all()
    )
    return {pid: int(total) for pid, total in rows}


def _ensure_ledger_open(db: Session, tournament_id: int) -> None:
    """登记/撤销的前置条件：全部已生成轮次结束且无待处理复核。"""
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
            detail=f"第 {open_round.round_no} 轮尚未结束，不能登记或撤销扣分",
        )
    if has_pending_review_for_tournament(db, tournament_id):
        raise HTTPException(
            status_code=409,
            detail="赛事存在待处理的赛果复核，需待复核关闭后才能登记或撤销扣分",
        )


def _check_event_seq_snapshot(
    db: Session, tournament_id: int, expected_event_seq: int
) -> None:
    """先完成全部资格校验，再在同一写事务内确认事件序号快照未变化。"""
    actual_seq = current_event_seq(db, tournament_id)
    if expected_event_seq != actual_seq:
        raise HTTPException(
            status_code=409,
            detail=(
                f"赛事快照已变化：当前最大事件序号为 {actual_seq}，"
                f"请求期望 {expected_event_seq}"
            ),
        )


def _find_replay(
    db: Session, tournament_id: int, request_id: str, fingerprint: dict
) -> PenaltyEntryResponse | None:
    """同 request_id 的已成功请求：参数一致返回首次响应，否则 409。"""
    entry = (
        db.query(PenaltyEntry)
        .filter(
            PenaltyEntry.tournament_id == tournament_id,
            PenaltyEntry.request_id == request_id,
        )
        .first()
    )
    if entry is None:
        return None
    if _entry_fingerprint(entry) == fingerprint:
        return PenaltyEntryResponse(**entry.response)
    raise HTTPException(
        status_code=409,
        detail="request_id 已被用于该赛事的其他扣分请求，请更换 request_id",
    )


def _entry_fingerprint(entry: PenaltyEntry) -> dict:
    """由不可变条目字段重建参数指纹（条目本身即幂等记录）。"""
    if entry.kind == DEDUCT:
        return {
            "kind": DEDUCT,
            "participant_id": entry.participant_id,
            "points_half": entry.points,
            "reason": entry.reason,
            "expected_event_seq": entry.expected_event_seq,
        }
    return {
        "kind": REVOKE,
        "revokes_entry_id": entry.revokes_entry_id,
        "reason": entry.reason,
        "expected_event_seq": entry.expected_event_seq,
    }


def serialize_entry(
    db: Session, entry: PenaltyEntry, *, participants: dict | None = None
) -> PenaltyEntryResponse:
    """组装账本条目响应（含参赛者编号与姓名）。"""
    if participants is None:
        participant = db.get(Participant, entry.participant_id)
    else:
        participant = participants[entry.participant_id]
    return PenaltyEntryResponse(
        id=entry.id,
        tournament_id=entry.tournament_id,
        participant_id=entry.participant_id,
        number=participant.number,
        name=participant.name,
        kind=entry.kind,
        points=half_to_float(entry.points),
        reason=entry.reason,
        status=entry.status,
        revokes_entry_id=entry.revokes_entry_id,
        expected_event_seq=entry.expected_event_seq,
        created_at=entry.created_at,
    )


def record_penalty(
    db: Session, tournament_id: int, req: PenaltyDeductRequest
) -> tuple[PenaltyEntryResponse, Event | None]:
    """登记一笔扣分（flush，不提交），返回（响应, 事件）。

    幂等重放时返回（首次响应, None），不产生任何写入；资格不符或
    事件序号快照冲突抛 409，且在任何写入之前抛出（不落库）。
    """
    points_half = round(req.points * 2)
    fingerprint = {
        "kind": DEDUCT,
        "participant_id": req.participant_id,
        "points_half": points_half,
        "reason": req.reason,
        "expected_event_seq": req.expected_event_seq,
    }
    # 幂等判定先于业务校验：重试已成功请求必须返回原结果
    replay = _find_replay(db, tournament_id, req.request_id, fingerprint)
    if replay is not None:
        return replay, None

    # 写锁可能经过等待才取得：以锁内已提交的最新状态重新校验
    participant = (
        db.query(Participant)
        .filter(
            Participant.id == req.participant_id,
            Participant.tournament_id == tournament_id,
        )
        .first()
    )
    if participant is None:
        raise HTTPException(status_code=404, detail="参赛者不存在")

    _ensure_ledger_open(db, tournament_id)
    _check_event_seq_snapshot(db, tournament_id, req.expected_event_seq)

    entry = PenaltyEntry(
        tournament_id=tournament_id,
        participant_id=participant.id,
        request_id=req.request_id,
        expected_event_seq=req.expected_event_seq,
        kind=DEDUCT,
        points=points_half,
        reason=req.reason,
        revokes_entry_id=None,
        status=ACTIVE,
        response={},  # 先占位，响应组装后回填（同事务）
    )
    db.add(entry)
    db.flush()  # 取得 entry.id，供事件与响应使用

    event = append_event(
        db,
        tournament_id,
        PENALTY_RECORDED,
        {
            "entry_id": entry.id,
            "participant_id": participant.id,
            "number": participant.number,
            "points": half_to_float(points_half),
            "reason": req.reason,
            "expected_event_seq": req.expected_event_seq,
        },
    )

    response = serialize_entry(db, entry)
    entry.response = response.model_dump(mode="json")
    db.flush()
    return response, event


def revoke_penalty(
    db: Session, entry_id: int, req: PenaltyRevokeRequest
) -> tuple[PenaltyEntryResponse, Event | None]:
    """以新条目撤销一笔仍生效的扣分（flush，不提交）。

    返回（响应, 事件）；幂等重放返回（首次响应, None）。目标不存在
    404；目标不是扣分条目或已被撤销、资格不符、快照冲突均 409，
    且在任何写入之前抛出（不落库）。
    """
    target = db.get(PenaltyEntry, entry_id)
    if target is None:
        raise HTTPException(status_code=404, detail="扣分记录不存在")
    tournament_id = target.tournament_id

    fingerprint = {
        "kind": REVOKE,
        "revokes_entry_id": entry_id,
        "reason": req.reason,
        "expected_event_seq": req.expected_event_seq,
    }
    # 幂等判定先于业务校验：重试已成功请求必须返回原结果
    replay = _find_replay(db, tournament_id, req.request_id, fingerprint)
    if replay is not None:
        return replay, None

    if target.kind != DEDUCT:
        raise HTTPException(
            status_code=409, detail="该记录不是扣分条目，不能撤销"
        )
    if target.status != ACTIVE:
        raise HTTPException(
            status_code=409, detail="该扣分已被撤销，不能重复撤销"
        )

    _ensure_ledger_open(db, tournament_id)
    _check_event_seq_snapshot(db, tournament_id, req.expected_event_seq)

    participant = db.get(Participant, target.participant_id)
    entry = PenaltyEntry(
        tournament_id=tournament_id,
        participant_id=target.participant_id,
        request_id=req.request_id,
        expected_event_seq=req.expected_event_seq,
        kind=REVOKE,
        # 冗余被撤销的量：账本单行即可读
        points=target.points,
        reason=req.reason,
        revokes_entry_id=target.id,
        status=APPLIED,
        response={},  # 先占位，响应组装后回填（同事务）
    )
    db.add(entry)
    # 原扣分保留并置为已撤销：与 revoke 条目同一事务生效
    target.status = REVOKED
    db.flush()  # 取得 entry.id，供事件与响应使用

    event = append_event(
        db,
        tournament_id,
        PENALTY_REVOKED,
        {
            "entry_id": entry.id,
            "revoked_entry_id": target.id,
            "participant_id": participant.id,
            "number": participant.number,
            "points": half_to_float(target.points),
            "reason": req.reason,
            "expected_event_seq": req.expected_event_seq,
        },
    )

    response = serialize_entry(db, entry)
    entry.response = response.model_dump(mode="json")
    db.flush()
    return response, event


def list_entries_for_tournament(
    db: Session, tournament_id: int
) -> list[PenaltyEntry]:
    """赛事下的全部账本条目（按登记顺序）。"""
    return list(
        db.query(PenaltyEntry)
        .filter(PenaltyEntry.tournament_id == tournament_id)
        .order_by(PenaltyEntry.id)
        .all()
    )


def ledger_for_participant(
    db: Session, tournament_id: int, participant_id: int
) -> ParticipantPenaltyLedgerResponse:
    """参赛者的完整账本：逐条记录 + 当前有效扣分合计。"""
    participant = (
        db.query(Participant)
        .filter(
            Participant.id == participant_id,
            Participant.tournament_id == tournament_id,
        )
        .first()
    )
    if participant is None:
        raise HTTPException(status_code=404, detail="参赛者不存在")

    entries = [
        e
        for e in list_entries_for_tournament(db, tournament_id)
        if e.participant_id == participant.id
    ]
    by_id = {participant.id: participant}
    effective = sum(
        e.points for e in entries if e.kind == DEDUCT and e.status == ACTIVE
    )
    return ParticipantPenaltyLedgerResponse(
        participant_id=participant.id,
        number=participant.number,
        name=participant.name,
        effective_penalty=half_to_float(effective),
        entries=[
            serialize_entry(db, e, participants=by_id) for e in entries
        ],
    )
