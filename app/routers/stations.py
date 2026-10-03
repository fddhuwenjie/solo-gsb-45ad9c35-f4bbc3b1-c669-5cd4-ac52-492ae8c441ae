"""工位与轮次工位方案接口。

- 工位维护：新增（单个/批量）、查询、更新可用状态与能力标签；
- 工位方案：POST 发布新版本（乐观并发 expected_version + 固定
  工位 pins），GET 按版本查询历史方案。

所有写请求与其他写请求一样经进程级写锁串行；方案发布与
station_plan.published 事件在同一事务中提交。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..events.service import commit_and_publish
from ..locking import writer_lock
from ..models import Round, Station, StationPlan
from ..schemas import (
    StationCreate,
    StationPlanCreate,
    StationPlanResponse,
    StationResponse,
    StationsBulkCreate,
    StationUpdate,
    StationAssignmentResponse,
    UnassignedMatchResponse,
)
from ..services.station_plans import create_station_plan
from ..services.lifecycle import ensure_tournament_writable
from .tournaments import get_tournament_or_404

router = APIRouter()


def serialize_plan(plan: StationPlan) -> StationPlanResponse:
    return StationPlanResponse(
        id=plan.id,
        round_id=plan.round_id,
        round_no=plan.round.round_no,
        version=plan.version,
        assignments=[
            StationAssignmentResponse(
                match_id=a.match_id, station_no=a.station_no
            )
            for a in plan.assignments
        ],
        unassigned=[UnassignedMatchResponse(**u) for u in plan.unassigned],
        created_at=plan.created_at,
    )


# ---------- 工位维护 ----------

def _add_stations(db: Session, tournament_id: int, items: list[StationCreate]):
    # 写请求进程级串行：重复检查与插入全程持锁
    with writer_lock():
        ensure_tournament_writable(db, tournament_id)
        problems: list[str] = []
        seen: set[int] = set()
        for item in items:
            if item.station_no in seen:
                problems.append(f"工位 {item.station_no} 在请求中重复")
            seen.add(item.station_no)
        existing = {
            row[0]
            for row in db.query(Station.station_no)
            .filter(
                Station.tournament_id == tournament_id,
                Station.station_no.in_(seen),
            )
            .all()
        }
        for no in sorted(existing):
            problems.append(f"工位 {no} 已存在")
        if problems:
            raise HTTPException(status_code=409, detail="；".join(problems))

        created = []
        for item in items:
            station = Station(
                tournament_id=tournament_id,
                station_no=item.station_no,
                available=item.available,
                tags=item.tags,
            )
            db.add(station)
            created.append(station)
        db.commit()
    for station in created:
        db.refresh(station)
    return created


@router.post(
    "/tournaments/{tournament_id}/stations",
    response_model=StationResponse,
    status_code=201,
)
def create_station(
    tournament_id: int,
    payload: StationCreate,
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)
    return _add_stations(db, tournament_id, [payload])[0]


@router.post(
    "/tournaments/{tournament_id}/stations/bulk",
    response_model=list[StationResponse],
    status_code=201,
)
def create_stations_bulk(
    tournament_id: int,
    payload: StationsBulkCreate,
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)
    return _add_stations(db, tournament_id, payload.stations)


@router.get(
    "/tournaments/{tournament_id}/stations",
    response_model=list[StationResponse],
)
def list_stations(tournament_id: int, db: Session = Depends(get_db)):
    get_tournament_or_404(db, tournament_id)
    return (
        db.query(Station)
        .filter(Station.tournament_id == tournament_id)
        .order_by(Station.station_no)
        .all()
    )


@router.put(
    "/tournaments/{tournament_id}/stations/{station_no}",
    response_model=StationResponse,
)
def update_station(
    tournament_id: int,
    station_no: int,
    payload: StationUpdate,
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)
    with writer_lock():
        ensure_tournament_writable(db, tournament_id)
        station = (
            db.query(Station)
            .filter(
                Station.tournament_id == tournament_id,
                Station.station_no == station_no,
            )
            .first()
        )
        if station is None:
            raise HTTPException(status_code=404, detail="工位不存在")
        station.available = payload.available
        station.tags = payload.tags
        db.commit()
        db.refresh(station)
        return station


# ---------- 轮次工位方案 ----------

def _get_round_or_404(db: Session, tournament_id: int, round_no: int) -> Round:
    round_obj = (
        db.query(Round)
        .filter(
            Round.tournament_id == tournament_id,
            Round.round_no == round_no,
        )
        .first()
    )
    if round_obj is None:
        raise HTTPException(status_code=404, detail="该轮次不存在")
    return round_obj


@router.post(
    "/tournaments/{tournament_id}/rounds/{round_no}/station-plans",
    response_model=StationPlanResponse,
    status_code=201,
)
def publish_station_plan(
    tournament_id: int,
    round_no: int,
    payload: StationPlanCreate,
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)
    # 写请求进程级串行：版本检查、固定工位校验、求解、事件序号与
    # 「提交→发布」全程持锁
    with writer_lock():
        ensure_tournament_writable(db, tournament_id)
        plan, event = create_station_plan(
            db,
            tournament_id,
            round_no,
            payload.expected_version,
            payload.pins,
        )
        commit_and_publish(db, [event])
        db.refresh(plan)
        return serialize_plan(plan)


@router.get(
    "/tournaments/{tournament_id}/rounds/{round_no}/station-plans",
    response_model=list[StationPlanResponse],
)
def list_station_plans(
    tournament_id: int,
    round_no: int,
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)
    round_obj = _get_round_or_404(db, tournament_id, round_no)
    plans = (
        db.query(StationPlan)
        .filter(StationPlan.round_id == round_obj.id)
        .order_by(StationPlan.version)
        .all()
    )
    return [serialize_plan(p) for p in plans]


@router.get(
    "/tournaments/{tournament_id}/rounds/{round_no}/station-plans/{version}",
    response_model=StationPlanResponse,
)
def get_station_plan(
    tournament_id: int,
    round_no: int,
    version: int,
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)
    round_obj = _get_round_or_404(db, tournament_id, round_no)
    plan = (
        db.query(StationPlan)
        .filter(
            StationPlan.round_id == round_obj.id,
            StationPlan.version == version,
        )
        .first()
    )
    if plan is None:
        raise HTTPException(status_code=404, detail="该版本的工位方案不存在")
    return serialize_plan(plan)
