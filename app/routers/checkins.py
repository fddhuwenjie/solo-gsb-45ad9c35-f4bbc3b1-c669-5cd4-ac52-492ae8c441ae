"""现场签到快照接口。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..events.service import commit_and_publish
from ..locking import writer_lock
from ..schemas import (
    CheckInConfirmRequest,
    CheckInSnapshotRequest,
    CheckInSnapshotResponse,
)
from ..services.checkins import (
    confirm_snapshot,
    get_snapshot,
    list_snapshots,
    serialize_snapshot,
    submit_snapshot,
)
from ..services.lifecycle import ensure_tournament_writable
from .tournaments import get_tournament_or_404

router = APIRouter()


def _submit(
    tournament_id: int,
    payload: CheckInSnapshotRequest,
    db: Session,
    round_no: int | None = None,
):
    if round_no is not None and round_no != payload.round_no:
        raise HTTPException(status_code=409, detail="路径轮次号与请求体不一致")
    get_tournament_or_404(db, tournament_id)
    with writer_lock():
        db.expire_all()
        ensure_tournament_writable(db, tournament_id)
        response, event = submit_snapshot(db, tournament_id, payload)
        # 幂等重放没有任何写入，直接返回首次结果
        if event is not None:
            commit_and_publish(db, [event])
    return response


@router.put(
    "/tournaments/{tournament_id}/checkin-snapshots",
    response_model=CheckInSnapshotResponse,
)
@router.post(
    "/tournaments/{tournament_id}/checkin-snapshots",
    response_model=CheckInSnapshotResponse,
)
def put_checkin_snapshot(
    tournament_id: int,
    payload: CheckInSnapshotRequest,
    db: Session = Depends(get_db),
):
    """创建或更新尚未生成的下一轮签到快照（幂等）。"""
    return _submit(tournament_id, payload, db)


@router.put(
    "/tournaments/{tournament_id}/rounds/{round_no}/checkin-snapshot",
    response_model=CheckInSnapshotResponse,
)
@router.post(
    "/tournaments/{tournament_id}/rounds/{round_no}/checkin-snapshot",
    response_model=CheckInSnapshotResponse,
)
def put_round_checkin_snapshot(
    tournament_id: int,
    round_no: int,
    payload: CheckInSnapshotRequest,
    db: Session = Depends(get_db),
):
    return _submit(tournament_id, payload, db, round_no)


def _confirm(
    tournament_id: int,
    payload: CheckInConfirmRequest,
    db: Session,
    round_no: int | None = None,
):
    if round_no is not None and round_no != payload.round_no:
        raise HTTPException(status_code=409, detail="路径轮次号与请求体不一致")
    get_tournament_or_404(db, tournament_id)
    with writer_lock():
        db.expire_all()
        ensure_tournament_writable(db, tournament_id)
        response, event = confirm_snapshot(db, tournament_id, payload)
        if event is not None:
            commit_and_publish(db, [event])
    return response


@router.post(
    "/tournaments/{tournament_id}/checkin-snapshots/confirm",
    response_model=CheckInSnapshotResponse,
)
def confirm_checkin_snapshot(
    tournament_id: int,
    payload: CheckInConfirmRequest,
    db: Session = Depends(get_db),
):
    """确认下一轮签到快照（幂等）。"""
    return _confirm(tournament_id, payload, db)


@router.post(
    "/tournaments/{tournament_id}/rounds/{round_no}/checkin-snapshot/confirm",
    response_model=CheckInSnapshotResponse,
)
def confirm_round_checkin_snapshot(
    tournament_id: int,
    round_no: int,
    payload: CheckInConfirmRequest,
    db: Session = Depends(get_db),
):
    return _confirm(tournament_id, payload, db, round_no)


@router.get(
    "/tournaments/{tournament_id}/checkin-snapshots",
    response_model=list[CheckInSnapshotResponse],
)
def list_checkin_snapshots(tournament_id: int, db: Session = Depends(get_db)):
    get_tournament_or_404(db, tournament_id)
    return [serialize_snapshot(s, db) for s in list_snapshots(db, tournament_id)]


@router.get(
    "/tournaments/{tournament_id}/checkin-snapshots/{round_no}",
    response_model=CheckInSnapshotResponse,
)
@router.get(
    "/tournaments/{tournament_id}/rounds/{round_no}/checkin-snapshot",
    response_model=CheckInSnapshotResponse,
)
def get_checkin_snapshot(
    tournament_id: int, round_no: int, db: Session = Depends(get_db)
):
    get_tournament_or_404(db, tournament_id)
    snapshot = get_snapshot(db, tournament_id, round_no)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="该轮次没有签到快照")
    return serialize_snapshot(snapshot, db)
