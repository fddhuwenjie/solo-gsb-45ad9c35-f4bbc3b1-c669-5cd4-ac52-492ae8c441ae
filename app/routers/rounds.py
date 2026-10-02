from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..events.service import (
    RESULT_SUBMITTED,
    ROUND_CLOSED,
    append_event,
    commit_and_publish,
)
from ..locking import writer_lock
from ..models import Match, Round
from ..schemas import ResultUpdate, RoundResponse
from ..services.clock import (
    freeze_clock_for_closed_round,
    get_clock,
    result_deadline_exceeded,
)
from ..services.result_reviews import pending_review_for_match
from ..services.rounds import generate_next_round
from .tournaments import get_tournament_or_404, serialize_round

router = APIRouter()


# 允许的对局得分（半分单位）
VALID_HALF_SCORES = {0, 1, 2}


@router.post(
    "/tournaments/{tournament_id}/rounds/next",
    response_model=RoundResponse,
    status_code=201,
)
def next_round(tournament_id: int, db: Session = Depends(get_db)):
    get_tournament_or_404(db, tournament_id)
    # 写请求进程级串行：open 轮检查、round_no 分配、事件序号与
    # 「提交→发布」全程持锁
    with writer_lock():
        round_obj, events = generate_next_round(db, tournament_id)
        commit_and_publish(db, events)
        db.refresh(round_obj)
        return serialize_round(round_obj)


@router.get(
    "/tournaments/{tournament_id}/rounds",
    response_model=list[RoundResponse],
)
def list_rounds(tournament_id: int, db: Session = Depends(get_db)):
    get_tournament_or_404(db, tournament_id)
    rounds = (
        db.query(Round)
        .filter(Round.tournament_id == tournament_id)
        .order_by(Round.round_no)
        .all()
    )
    return [serialize_round(r) for r in rounds]


@router.get(
    "/tournaments/{tournament_id}/rounds/{round_no}",
    response_model=RoundResponse,
)
def get_round(tournament_id: int, round_no: int, db: Session = Depends(get_db)):
    get_tournament_or_404(db, tournament_id)
    r = (
        db.query(Round)
        .filter(
            Round.tournament_id == tournament_id,
            Round.round_no == round_no,
        )
        .first()
    )
    if r is None:
        raise HTTPException(status_code=404, detail="该轮次不存在")
    return serialize_round(r)


def _try_close_round(db: Session, round_obj: Round) -> None:
    """所有普通对局均已录入赛果时自动结束该轮。"""
    db.flush()  # 确保本请求刚写入的得分对下面的查询可见
    unfinished = (
        db.query(Match)
        .filter(
            Match.round_id == round_obj.id,
            Match.player_black_id.is_not(None),
            Match.score_white.is_(None),
        )
        .count()
    )
    if unfinished == 0:
        round_obj.status = "closed"


@router.put(
    "/matches/{match_id}/result",
    response_model=RoundResponse,
)
def submit_result(
    match_id: int,
    payload: ResultUpdate,
    db: Session = Depends(get_db),
):
    match = db.get(Match, match_id)
    if match is None:
        raise HTTPException(status_code=404, detail="对局不存在")
    if match.player_black_id is None:
        raise HTTPException(status_code=409, detail="轮空对局无需录入赛果")

    sw = round(payload.score_white * 2)
    sb = round(payload.score_black * 2)
    if sw not in VALID_HALF_SCORES or sb not in VALID_HALF_SCORES:
        raise HTTPException(status_code=422, detail="每场得分只能是 0、0.5 或 1")
    if sw + sb != 2:
        raise HTTPException(status_code=422, detail="双方得分之和必须为 1")

    # 写请求进程级串行：锁可能经过等待才取得，先让锁外读取的对象
    # 过期，在锁内按已提交的最新状态重新读取（避免与并发生成轮次/
    # 赛果请求竞争），再完成写入、序号分配与「提交→发布」
    with writer_lock():
        db.expire_all()
        match = db.get(Match, match_id)
        round_obj = db.get(Round, match.round_id)
        if round_obj.status != "open":
            raise HTTPException(status_code=409, detail="该轮次已结束，不能修改赛果")

        # 该场存在待处理复核时冻结原比分：复核关闭前不能修改
        if pending_review_for_match(db, match_id) is not None:
            raise HTTPException(
                status_code=409,
                detail="该对局存在待处理复核，复核关闭前不能修改赛果",
            )

        # 计时已启动的轮次：晚于该场截止时刻的赛果拒绝录入
        clock = get_clock(db, round_obj.id)
        if result_deadline_exceeded(clock, match.id):
            raise HTTPException(
                status_code=409,
                detail="该场对局已超过截止时刻，不能录入赛果",
            )

        match.score_white = sw
        match.score_black = sb
        _try_close_round(db, round_obj)
        # 封盘时冻结该轮时钟（ended），剩余时间停在封盘时刻
        if round_obj.status == "closed":
            freeze_clock_for_closed_round(db, round_obj.id)

        events = [
            append_event(
                db,
                round_obj.tournament_id,
                RESULT_SUBMITTED,
                {
                    "match_id": match.id,
                    "round_id": round_obj.id,
                    "round_no": round_obj.round_no,
                    "player_white_id": match.player_white_id,
                    "player_black_id": match.player_black_id,
                    "score_white": sw / 2.0,
                    "score_black": sb / 2.0,
                },
            )
        ]
        # 本次录入使整轮封盘时，在同一事务中紧随其后记录 closed 事件
        if round_obj.status == "closed":
            events.append(
                append_event(
                    db,
                    round_obj.tournament_id,
                    ROUND_CLOSED,
                    {
                        "round_id": round_obj.id,
                        "round_no": round_obj.round_no,
                        "reason": "all_results_submitted",
                    },
                )
            )

        commit_and_publish(db, events)
        db.refresh(round_obj)
        return serialize_round(round_obj)
