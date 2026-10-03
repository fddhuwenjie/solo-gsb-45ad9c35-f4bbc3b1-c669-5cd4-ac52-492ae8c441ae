from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..events.service import (
    PARTICIPANT_REGISTERED,
    TOURNAMENT_CREATED,
    append_event,
    commit_and_publish,
)
from ..locking import writer_lock
from ..models import CheckInSnapshot, Match, Participant, Round, Tournament
from ..schemas import (
    MatchResponse,
    ParticipantCreate,
    ParticipantResponse,
    ParticipantsBulkCreate,
    ParticipantTagsUpdate,
    RoundResponse,
    StandingResponse,
    TournamentCreate,
    TournamentResponse,
    TournamentStatusControlRequest,
    TournamentStatusResponse,
)
from ..services.lifecycle import apply_tournament_status, ensure_tournament_writable
from ..services.scoring import compute_records, half_to_float

router = APIRouter()


def get_tournament_or_404(db: Session, tournament_id: int) -> Tournament:
    obj = db.get(Tournament, tournament_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="赛事不存在")
    return obj


def serialize_match(m: Match) -> MatchResponse:
    return MatchResponse(
        id=m.id,
        player_white=(
            ParticipantResponse.model_validate(m.white) if m.white else None
        ),
        player_black=(
            ParticipantResponse.model_validate(m.black) if m.black else None
        ),
        score_white=half_to_float(m.score_white) if m.score_white is not None else None,
        score_black=half_to_float(m.score_black) if m.score_black is not None else None,
        is_bye=m.player_black_id is None,
    )


def serialize_round(r: Round) -> RoundResponse:
    return RoundResponse(
        id=r.id,
        round_no=r.round_no,
        status=r.status,
        matches=[serialize_match(m) for m in r.matches],
    )


# ---------- Tournament ----------

@router.post("/tournaments", response_model=TournamentResponse, status_code=201)
def create_tournament(payload: TournamentCreate, db: Session = Depends(get_db)):
    # 所有写请求（含赛事创建）进程级串行：避免 SQLite 共享锁升级
    # 写锁的死锁型 BUSY，并保证提交与发布顺序一致
    with writer_lock():
        obj = Tournament(name=payload.name)
        db.add(obj)
        db.flush()  # 取得 obj.id，供事件使用
        # 赛事创建事件与赛事行同一事务提交；锁内完成「提交→发布」，
        # 与该赛事后续所有写请求严格按序
        event = append_event(db, obj.id, TOURNAMENT_CREATED, {})
        commit_and_publish(db, [event])
    db.refresh(obj)
    return obj


@router.get("/tournaments", response_model=list[TournamentResponse])
def list_tournaments(db: Session = Depends(get_db)):
    return db.query(Tournament).order_by(Tournament.id).all()


@router.get("/tournaments/{tournament_id}", response_model=TournamentResponse)
def get_tournament(tournament_id: int, db: Session = Depends(get_db)):
    return get_tournament_or_404(db, tournament_id)


@router.post(
    "/tournaments/{tournament_id}/status",
    response_model=TournamentStatusResponse,
)
def control_tournament_status(
    tournament_id: int,
    payload: TournamentStatusControlRequest,
    db: Session = Depends(get_db),
):
    """切换赛事状态（pause/resume/archive，幂等 + 序号快照）。

    active 与 paused 可互相切换；archive 只能从 paused 且所有轮次
    已结束、无待处理复核时进入，归档后永久只读。pause 在同一事务
    冻结所有运行中的轮次时钟，resume 只恢复被赛事暂停的时钟。
    """
    tournament = get_tournament_or_404(db, tournament_id)
    # 写请求进程级串行：重放判定、状态机校验、时钟冻结/恢复、事件
    # 序号与「提交→发布」全程持锁
    with writer_lock():
        db.expire_all()
        tournament = get_tournament_or_404(db, tournament_id)
        response, event = apply_tournament_status(db, tournament, payload)
        # 幂等重放（event 为 None）没有任何写入，直接返回首次结果
        if event is not None:
            commit_and_publish(db, [event])
        return response


# ---------- Participant ----------

def _ensure_registration_open(db: Session, tournament_id: int) -> None:
    has_round = (
        db.query(Round.id)
        .filter(Round.tournament_id == tournament_id)
        .first()
    )
    has_first_round_checkin = (
        db.query(CheckInSnapshot.id)
        .filter(
            CheckInSnapshot.tournament_id == tournament_id,
            CheckInSnapshot.round_no == 1,
        )
        .first()
    )
    if has_round is not None or has_first_round_checkin is not None:
        raise HTTPException(
            status_code=409,
            detail="首轮签到快照建立或首轮生成后不允许再增删参赛者",
        )


def _add_participants(
    db: Session, tournament_id: int, entries: list[tuple[str, list[str]]]
):
    # 写请求进程级串行：count 读 → 编号分配 → 事件序号 → 提交发布
    # 全程持锁，并发报名既不会撞编号唯一约束，也不会交错序号。
    with writer_lock():
        ensure_tournament_writable(db, tournament_id)
        _ensure_registration_open(db, tournament_id)
        count = (
            db.query(Participant)
            .filter(Participant.tournament_id == tournament_id)
            .count()
        )
        created = []
        events = []
        for i, (name, required_tags) in enumerate(entries, start=count + 1):
            p = Participant(
                tournament_id=tournament_id,
                number=i,
                name=name,
                required_tags=required_tags,
            )
            db.add(p)
            db.flush()  # 取得 p.id，供事件使用
            created.append(p)
            # 每位参赛者一条事件，按录入顺序占用连续序号，同事务提交
            events.append(
                append_event(
                    db,
                    tournament_id,
                    PARTICIPANT_REGISTERED,
                    {
                        "participant_id": p.id,
                        "number": p.number,
                        "name": p.name,
                    },
                )
            )
        commit_and_publish(db, events)
    for p in created:
        db.refresh(p)
    return created


@router.post(
    "/tournaments/{tournament_id}/participants",
    response_model=list[ParticipantResponse],
    status_code=201,
)
def create_participant(
    tournament_id: int,
    payload: ParticipantCreate,
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)
    return _add_participants(
        db, tournament_id, [(payload.name, payload.required_tags)]
    )


@router.post(
    "/tournaments/{tournament_id}/participants/bulk",
    response_model=list[ParticipantResponse],
    status_code=201,
)
def create_participants_bulk(
    tournament_id: int,
    payload: ParticipantsBulkCreate,
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)
    return _add_participants(
        db, tournament_id, [(name, []) for name in payload.names]
    )


@router.get(
    "/tournaments/{tournament_id}/participants",
    response_model=list[ParticipantResponse],
)
def list_participants(tournament_id: int, db: Session = Depends(get_db)):
    get_tournament_or_404(db, tournament_id)
    return (
        db.query(Participant)
        .filter(Participant.tournament_id == tournament_id)
        .order_by(Participant.number)
        .all()
    )


@router.put(
    "/tournaments/{tournament_id}/participants/{number}/tags",
    response_model=ParticipantResponse,
)
def update_participant_tags(
    tournament_id: int,
    number: int,
    payload: ParticipantTagsUpdate,
    db: Session = Depends(get_db),
):
    """登记/更新参赛者的必需标签（首轮生成后仍可调整）。"""
    get_tournament_or_404(db, tournament_id)
    with writer_lock():
        ensure_tournament_writable(db, tournament_id)
        participant = (
            db.query(Participant)
            .filter(
                Participant.tournament_id == tournament_id,
                Participant.number == number,
            )
            .first()
        )
        if participant is None:
            raise HTTPException(status_code=404, detail="参赛者不存在")
        participant.required_tags = payload.required_tags
        db.commit()
        db.refresh(participant)
        return participant


# ---------- Standings ----------

@router.get(
    "/tournaments/{tournament_id}/standings",
    response_model=list[StandingResponse],
)
def get_standings(tournament_id: int, db: Session = Depends(get_db)):
    get_tournament_or_404(db, tournament_id)
    records = compute_records(db, tournament_id, include_open=True)
    return [
        StandingResponse(
            rank=i,
            participant_id=r.participant_id,
            number=r.number,
            name=r.name,
            points=half_to_float(r.points),
            penalty=half_to_float(r.penalty),
            rank_points=half_to_float(r.rank_points),
            buchholz=half_to_float(r.buchholz),
            played=r.played,
            byes=r.byes,
        )
        for i, r in enumerate(records, start=1)
    ]
