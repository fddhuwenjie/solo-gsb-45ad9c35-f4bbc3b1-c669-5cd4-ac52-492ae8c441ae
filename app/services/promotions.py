"""晋级名单：冻结发布、放弃与候补递补。

- publish_promotion_list：仅允许在「所有已生成轮次均结束且没有待处理
  赛果复核」时发布。同一事务内重算资格轮次（含）之前的排名积分与
  Buchholz，按排名积分 → Buchholz → 参赛者编号固定裁决，冻结前
  slots 名为晋级席位、其余参赛者按同一次序为候补，并写入
  promotion.published 事件。事件序号变化、名额非法（超过参赛人数）、
  资格轮次不存在、仍有未完成轮次或有待处理复核时拒绝且不留记录。
  名单发布后，原排名与扣分变化不改写该版本（依据已冻结在席位/候补行）。
- forfeit_promotion_seat：对已冻结名单标记放弃。只有当前有效晋级者
  （某席位的当前持有者）可被标记；同一事务内把最前候补（position
  最小且仍为 waiting）提升为该席位当前持有者，写入
  promotion.substituted 事件；无候补可递补时席位保持空席。
- 幂等：request_id 在赛事内唯一（publish 与 forfeit 共用同一命名
  空间）。重复请求参数一致时原样返回首次响应快照（不产生任何写入与
  事件）；同键不同参数返回 409。

注意：本模块的写函数只在调用方事务内写入并 flush，不提交；名单版本、
席位/候补变更、幂等记录与 promotion.* 事件由路由层在同一事务中统一
提交。所有调用均处于进程级 writer_lock 内，并由
(tournament_id, request_id)、(tournament_id, version) 等唯一约束在
数据库层兜底。
"""
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..events.service import (
    PROMOTION_PUBLISHED,
    PROMOTION_SUBSTITUTED,
    append_event,
)
from ..models import (
    Event,
    Participant,
    PromotionAction,
    PromotionList,
    PromotionSeat,
    PromotionWaitlistEntry,
    Round,
)
from ..schemas import (
    PromotionForfeitRequest,
    PromotionForfeitResponse,
    PromotionListResponse,
    PromotionParticipantSnapshot,
    PromotionPublishRequest,
    PromotionSeatCurrent,
    PromotionSeatResponse,
    PromotionWaitlistEntryResponse,
)
from .announcements import current_event_seq
from .lifecycle import ensure_tournament_writable
from .result_reviews import has_pending_review_for_tournament
from .scoring import compute_records, half_to_float

PUBLISH = "publish"
FORFEIT = "forfeit"
SEAT_ACTIVE = "active"
SEAT_VACANT = "vacant"
WAITING = "waiting"
PROMOTED = "promoted"
SOURCE_ORIGINAL = "original"
SOURCE_WAITLIST = "waitlist"


# ---------- 快照与序列化 ----------

def _snapshot(
    participant: Participant, rank: int, rank_points: int, buchholz: int
) -> PromotionParticipantSnapshot:
    """由参赛者行与冻结依据（半分单位）组装快照。"""
    return PromotionParticipantSnapshot(
        participant_id=participant.id,
        number=participant.number,
        name=participant.name,
        rank=rank,
        rank_points=half_to_float(rank_points),
        buchholz=half_to_float(buchholz),
    )


def _seat_current_snapshot(
    seat: PromotionSeat,
    participants: dict[int, Participant],
    waitlist_by_participant: dict[int, PromotionWaitlistEntry],
) -> PromotionSeatCurrent | None:
    """席位当前持有者快照：依据取原始冻结值或候补冻结值。"""
    if seat.current_participant_id is None:
        return None
    pid = seat.current_participant_id
    participant = participants[pid]
    if pid == seat.original_participant_id:
        rank, rank_points, buchholz = (
            seat.original_rank,
            seat.original_rank_points,
            seat.original_buchholz,
        )
        source = SOURCE_ORIGINAL
    else:
        entry = waitlist_by_participant[pid]
        rank, rank_points, buchholz = (
            entry.rank,
            entry.rank_points,
            entry.buchholz,
        )
        source = SOURCE_WAITLIST
    return PromotionSeatCurrent(
        participant_id=participant.id,
        number=participant.number,
        name=participant.name,
        rank=rank,
        rank_points=half_to_float(rank_points),
        buchholz=half_to_float(buchholz),
        source=source,
    )


def serialize_list(db: Session, plist: PromotionList) -> PromotionListResponse:
    """组装名单版本响应：每席位原始/当前参赛者、状态与冻结依据。"""
    seats = (
        db.query(PromotionSeat)
        .filter(PromotionSeat.list_id == plist.id)
        .order_by(PromotionSeat.seat_no)
        .all()
    )
    waitlist = (
        db.query(PromotionWaitlistEntry)
        .filter(PromotionWaitlistEntry.list_id == plist.id)
        .order_by(PromotionWaitlistEntry.position)
        .all()
    )
    participant_ids = {s.original_participant_id for s in seats}
    participant_ids.update(
        s.current_participant_id
        for s in seats
        if s.current_participant_id is not None
    )
    participant_ids.update(w.participant_id for w in waitlist)
    participants = {
        p.id: p
        for p in db.query(Participant)
        .filter(Participant.id.in_(participant_ids))
        .all()
    }
    waitlist_by_participant = {w.participant_id: w for w in waitlist}

    return PromotionListResponse(
        id=plist.id,
        tournament_id=plist.tournament_id,
        version=plist.version,
        qualifying_round_no=plist.qualifying_round_no,
        slots=plist.slots,
        expected_event_seq=plist.expected_event_seq,
        seats=[
            PromotionSeatResponse(
                seat_no=s.seat_no,
                status=s.status,
                original=_snapshot(
                    participants[s.original_participant_id],
                    s.original_rank,
                    s.original_rank_points,
                    s.original_buchholz,
                ),
                current=_seat_current_snapshot(
                    s, participants, waitlist_by_participant
                ),
            )
            for s in seats
        ],
        waitlist=[
            PromotionWaitlistEntryResponse(
                position=w.position,
                participant=_snapshot(
                    participants[w.participant_id],
                    w.rank,
                    w.rank_points,
                    w.buchholz,
                ),
                status=w.status,
            )
            for w in waitlist
        ],
        created_at=plist.created_at,
    )


# ---------- 幂等与前置校验 ----------

def _find_replay(
    db: Session, tournament_id: int, request_id: str, fingerprint: dict
) -> PromotionAction | None:
    """同 request_id 的已成功请求：参数一致返回该记录，否则 409。"""
    action = (
        db.query(PromotionAction)
        .filter(
            PromotionAction.tournament_id == tournament_id,
            PromotionAction.request_id == request_id,
        )
        .first()
    )
    if action is None:
        return None
    if action.params == fingerprint:
        return action
    raise HTTPException(
        status_code=409,
        detail="request_id 已被用于该赛事的其他晋级名单请求，请更换 request_id",
    )


def _ensure_publishable(db: Session, tournament_id: int) -> None:
    """发布前置条件：全部已生成轮次结束且无待处理复核。"""
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
            detail=f"第 {open_round.round_no} 轮尚未结束，不能发布晋级名单",
        )
    if has_pending_review_for_tournament(db, tournament_id):
        raise HTTPException(
            status_code=409,
            detail="赛事存在待处理的赛果复核，需待复核关闭后才能发布晋级名单",
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


# ---------- 发布 ----------

def publish_promotion_list(
    db: Session, tournament_id: int, req: PromotionPublishRequest
) -> tuple[PromotionListResponse, Event | None]:
    """发布一份晋级名单（flush，不提交），返回（响应, 事件）。

    幂等重放时返回（首次响应, None），不产生任何写入；资格不符、
    名额非法或事件序号快照冲突抛 409，且在任何写入之前抛出（不落库）。
    """
    # 维护态/归档赛事拒绝一切写操作（在幂等判定与任何写入之前）
    ensure_tournament_writable(db, tournament_id)
    fingerprint = {
        "action": PUBLISH,
        "qualifying_round_no": req.qualifying_round_no,
        "slots": req.slots,
        "expected_event_seq": req.expected_event_seq,
    }
    # 幂等判定先于业务校验：重试已成功请求必须返回原结果
    replay = _find_replay(db, tournament_id, req.request_id, fingerprint)
    if replay is not None:
        return PromotionListResponse(**replay.response), None

    # 写锁可能经过等待才取得：以锁内已提交的最新状态重新校验
    _ensure_publishable(db, tournament_id)
    qualifying = (
        db.query(Round)
        .filter(
            Round.tournament_id == tournament_id,
            Round.round_no == req.qualifying_round_no,
        )
        .first()
    )
    if qualifying is None:
        raise HTTPException(
            status_code=409,
            detail=f"资格轮次 {req.qualifying_round_no} 不存在",
        )

    # 同一事务内重算当时的排名积分与 Buchholz（只统计已结束轮次；
    # 前置条件已保证资格轮次及之前轮次全部结束）
    records = compute_records(
        db,
        tournament_id,
        include_open=False,
        through_round_no=req.qualifying_round_no,
    )
    if req.slots > len(records):
        raise HTTPException(
            status_code=409,
            detail=f"晋级名额 {req.slots} 超过参赛人数 {len(records)}",
        )
    _check_event_seq_snapshot(db, tournament_id, req.expected_event_seq)

    version = (
        db.query(func.max(PromotionList.version))
        .filter(PromotionList.tournament_id == tournament_id)
        .scalar()
        or 0
    ) + 1
    plist = PromotionList(
        tournament_id=tournament_id,
        version=version,
        qualifying_round_no=req.qualifying_round_no,
        slots=req.slots,
        expected_event_seq=req.expected_event_seq,
    )
    db.add(plist)
    db.flush()  # 取得 plist.id，供席位/候补/事件使用

    # 固定裁决：排名积分 → Buchholz → 编号（compute_records 已按此排序）；
    # 前 slots 名为晋级席位，其余按同一次序为候补
    for i, rec in enumerate(records[: req.slots], start=1):
        db.add(
            PromotionSeat(
                list_id=plist.id,
                seat_no=i,
                original_participant_id=rec.participant_id,
                original_rank=i,
                original_rank_points=rec.rank_points,
                original_buchholz=rec.buchholz,
                current_participant_id=rec.participant_id,
                status=SEAT_ACTIVE,
            )
        )
    for j, rec in enumerate(records[req.slots :], start=1):
        db.add(
            PromotionWaitlistEntry(
                list_id=plist.id,
                position=j,
                participant_id=rec.participant_id,
                rank=req.slots + j,
                rank_points=rec.rank_points,
                buchholz=rec.buchholz,
                status=WAITING,
            )
        )
    db.flush()

    response = serialize_list(db, plist)
    db.add(
        PromotionAction(
            tournament_id=tournament_id,
            list_id=plist.id,
            request_id=req.request_id,
            action=PUBLISH,
            params=fingerprint,
            response=response.model_dump(mode="json"),
        )
    )

    event = append_event(
        db,
        tournament_id,
        PROMOTION_PUBLISHED,
        {
            "list_id": plist.id,
            "version": version,
            "qualifying_round_no": req.qualifying_round_no,
            "slots": req.slots,
            "expected_event_seq": req.expected_event_seq,
            "seats": [
                {
                    "seat_no": s.seat_no,
                    "participant_id": s.original.participant_id,
                    "number": s.original.number,
                    "rank": s.original.rank,
                    "rank_points": s.original.rank_points,
                    "buchholz": s.original.buchholz,
                }
                for s in response.seats
            ],
            "waitlist": [
                {
                    "position": w.position,
                    "participant_id": w.participant.participant_id,
                    "number": w.participant.number,
                    "rank": w.participant.rank,
                    "rank_points": w.participant.rank_points,
                    "buchholz": w.participant.buchholz,
                }
                for w in response.waitlist
            ],
        },
    )
    db.flush()
    return response, event


# ---------- 放弃与递补 ----------

def forfeit_promotion_seat(
    db: Session, list_id: int, req: PromotionForfeitRequest
) -> tuple[PromotionForfeitResponse, Event | None]:
    """标记当前有效晋级者放弃并递补（flush，不提交）。

    返回（响应, 事件）；幂等重放返回（首次响应, None）。名单不存在
    404；参赛者不存在 404；不是当前有效晋级者 409，且在任何写入之前
    抛出（不落库）。
    """
    plist = db.get(PromotionList, list_id)
    if plist is None:
        raise HTTPException(status_code=404, detail="晋级名单不存在")
    tournament_id = plist.tournament_id
    # 维护态/归档赛事拒绝一切写操作（在幂等判定与任何写入之前）
    ensure_tournament_writable(db, tournament_id)

    fingerprint = {
        "action": FORFEIT,
        "list_id": list_id,
        "participant_id": req.participant_id,
        "reason": req.reason,
    }
    # 幂等判定先于业务校验：重试已成功请求必须返回原结果
    replay = _find_replay(db, tournament_id, req.request_id, fingerprint)
    if replay is not None:
        return PromotionForfeitResponse(**replay.response), None

    participant = db.get(Participant, req.participant_id)
    if participant is None or participant.tournament_id != tournament_id:
        raise HTTPException(status_code=404, detail="参赛者不存在")

    # 只有当前有效晋级者（某席位的当前持有者）可以标记放弃
    seat = (
        db.query(PromotionSeat)
        .filter(
            PromotionSeat.list_id == list_id,
            PromotionSeat.current_participant_id == req.participant_id,
            PromotionSeat.status == SEAT_ACTIVE,
        )
        .first()
    )
    if seat is None:
        raise HTTPException(
            status_code=409,
            detail="该参赛者不是该名单的当前有效晋级者，不能标记放弃",
        )

    waitlist = (
        db.query(PromotionWaitlistEntry)
        .filter(PromotionWaitlistEntry.list_id == list_id)
        .order_by(PromotionWaitlistEntry.position)
        .all()
    )
    waitlist_by_participant = {w.participant_id: w for w in waitlist}
    participants = {
        p.id: p
        for p in db.query(Participant)
        .filter(
            Participant.id.in_(
                {seat.original_participant_id, req.participant_id}
                | {w.participant_id for w in waitlist}
            )
        )
        .all()
    }
    forfeited = _seat_current_snapshot(seat, participants, waitlist_by_participant)
    # 查询条件已限定 current_participant_id == req.participant_id，必非空
    assert forfeited is not None

    # 同一事务内把最前候补提升为该席位；无候补时保持空席
    now = datetime.utcnow()
    nxt = next((w for w in waitlist if w.status == WAITING), None)
    promoted: PromotionSeatCurrent | None = None
    if nxt is not None:
        nxt.status = PROMOTED
        nxt.updated_at = now
        seat.current_participant_id = nxt.participant_id
        promoted_participant = participants[nxt.participant_id]
        promoted = PromotionSeatCurrent(
            participant_id=promoted_participant.id,
            number=promoted_participant.number,
            name=promoted_participant.name,
            rank=nxt.rank,
            rank_points=half_to_float(nxt.rank_points),
            buchholz=half_to_float(nxt.buchholz),
            source=SOURCE_WAITLIST,
        )
    else:
        seat.current_participant_id = None
        seat.status = SEAT_VACANT
    seat.updated_at = now
    db.flush()

    response = PromotionForfeitResponse(
        list_id=plist.id,
        tournament_id=tournament_id,
        version=plist.version,
        seat_no=seat.seat_no,
        seat_status=seat.status,
        forfeited=forfeited,
        promoted=promoted,
        reason=req.reason,
        created_at=now,
    )
    db.add(
        PromotionAction(
            tournament_id=tournament_id,
            list_id=plist.id,
            request_id=req.request_id,
            action=FORFEIT,
            params=fingerprint,
            response=response.model_dump(mode="json"),
        )
    )

    event = append_event(
        db,
        tournament_id,
        PROMOTION_SUBSTITUTED,
        {
            "list_id": plist.id,
            "version": plist.version,
            "seat_no": seat.seat_no,
            "seat_status": seat.status,
            "forfeited_participant_id": forfeited.participant_id,
            "forfeited_number": forfeited.number,
            "promoted_participant_id": (
                promoted.participant_id if promoted is not None else None
            ),
            "promoted_number": promoted.number if promoted is not None else None,
            "reason": req.reason,
        },
    )
    db.flush()
    return response, event


# ---------- 查询 ----------

def list_lists(db: Session, tournament_id: int) -> list[PromotionList]:
    """赛事下的全部晋级名单版本（按版本升序）。"""
    return list(
        db.query(PromotionList)
        .filter(PromotionList.tournament_id == tournament_id)
        .order_by(PromotionList.version)
        .all()
    )


def get_list_by_version(
    db: Session, tournament_id: int, version: int
) -> PromotionList | None:
    return (
        db.query(PromotionList)
        .filter(
            PromotionList.tournament_id == tournament_id,
            PromotionList.version == version,
        )
        .first()
    )
