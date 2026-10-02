"""赛务争议扣分账本接口。

- POST /tournaments/{id}/penalties：为参赛者登记一笔扣分。请求携带
  幂等键 request_id、乐观快照 expected_event_seq、扣分量（0.5 的
  整数倍）与理由；仅允许在所有已生成轮次结束且没有待处理赛果复核
  时登记。成功时在同一写事务内写入 penalty.recorded 事件。
- POST /penalties/{entry_id}/revoke：以一条新记录撤销一笔仍生效的
  扣分；原扣分保留并置为 revoked，与 penalty.revoked 事件同一事务
  提交。重复撤销或目标不是扣分条目返回 409 且不落库。
- GET /tournaments/{id}/penalties：赛事下的完整账本（按登记顺序）。
- GET /tournaments/{id}/participants/{pid}/penalties：参赛者的完整
  账本（逐条记录 + 当前有效扣分合计）。

重复 request_id（参数一致）原样返回首次结果，不产生新写入与事件；
同键不同参数返回 409。快照冲突、资格不符一律 409 且不落库。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..events.service import commit_and_publish
from ..locking import writer_lock
from ..models import PenaltyEntry
from ..schemas import (
    ParticipantPenaltyLedgerResponse,
    PenaltyDeductRequest,
    PenaltyEntryResponse,
    PenaltyRevokeRequest,
)
from ..services.penalties import (
    ledger_for_participant,
    list_entries_for_tournament,
    record_penalty,
    revoke_penalty,
    serialize_entry,
)
from .tournaments import get_tournament_or_404

router = APIRouter()


@router.post(
    "/tournaments/{tournament_id}/penalties",
    response_model=PenaltyEntryResponse,
    status_code=201,
)
def deduct(
    tournament_id: int,
    payload: PenaltyDeductRequest,
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)
    # 写请求进程级串行：资格校验、快照确认、条目落库、事件序号与
    # 「提交→发布」全程持锁
    with writer_lock():
        db.expire_all()
        response, event = record_penalty(db, tournament_id, payload)
        # 幂等重放（event 为 None）没有任何写入，直接返回首次结果
        if event is not None:
            commit_and_publish(db, [event])
    return response


@router.post(
    "/penalties/{entry_id}/revoke",
    response_model=PenaltyEntryResponse,
    status_code=201,
)
def revoke(
    entry_id: int,
    payload: PenaltyRevokeRequest,
    db: Session = Depends(get_db),
):
    # 存在性先在锁外给出 404；锁内会以最新状态重新读取并校验
    if db.get(PenaltyEntry, entry_id) is None:
        raise HTTPException(status_code=404, detail="扣分记录不存在")

    with writer_lock():
        db.expire_all()
        response, event = revoke_penalty(db, entry_id, payload)
        # 幂等重放（event 为 None）没有任何写入，直接返回首次结果
        if event is not None:
            commit_and_publish(db, [event])
    return response


@router.get(
    "/tournaments/{tournament_id}/penalties",
    response_model=list[PenaltyEntryResponse],
)
def list_ledger(tournament_id: int, db: Session = Depends(get_db)):
    get_tournament_or_404(db, tournament_id)
    return [
        serialize_entry(db, e)
        for e in list_entries_for_tournament(db, tournament_id)
    ]


@router.get(
    "/tournaments/{tournament_id}/participants/{participant_id}/penalties",
    response_model=ParticipantPenaltyLedgerResponse,
)
def participant_ledger(
    tournament_id: int, participant_id: int, db: Session = Depends(get_db)
):
    get_tournament_or_404(db, tournament_id)
    return ledger_for_participant(db, tournament_id, participant_id)
