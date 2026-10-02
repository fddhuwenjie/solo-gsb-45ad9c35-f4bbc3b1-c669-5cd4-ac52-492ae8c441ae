"""已提交赛果复核接口。

- POST /matches/{id}/result-reviews：为一场对局创建待处理复核。
  请求携带幂等键 request_id 与乐观快照 expected_event_seq、拟更正
  比分与理由；仅允许非轮空、已提交且尚未生成下一轮的对局，且该场
  不得已有待处理复核。创建后原比分冻结：复核待处理期间不能修改
  赛果、赛事不能生成下一轮。
- POST /result-reviews/{id}/decisions：对局双方分别同意/驳回。
  participant_id 必须是本场选手；重复决定返回原结果，pending
  期间改票原地更新。任一驳回即关闭（rejected，比分不变）；双方
  同意时在同一写事务内更正比分、关闭复核（approved，可见排名
  随之重算）并写入复核关闭事件。
- GET /result-reviews/{id}：复核详情（原比分、提议、决定、状态）。
- GET /tournaments/{id}/result-reviews：赛事下的复核列表。
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..events.service import commit_and_publish
from ..locking import writer_lock
from ..models import Match
from ..schemas import (
    ResultReviewCreate,
    ResultReviewDecisionRequest,
    ResultReviewResponse,
)
from ..services.result_reviews import (
    create_result_review,
    decide_result_review,
    get_review_or_404,
    list_reviews_for_tournament,
    serialize_review,
)
from .tournaments import get_tournament_or_404

router = APIRouter()


@router.post(
    "/matches/{match_id}/result-reviews",
    response_model=ResultReviewResponse,
    status_code=201,
)
def create_review(
    match_id: int,
    payload: ResultReviewCreate,
    db: Session = Depends(get_db),
):
    # 存在性先在锁外给出 404；锁内会以最新状态重新读取并校验
    match = db.get(Match, match_id)
    if match is None:
        raise HTTPException(status_code=404, detail="对局不存在")

    # 写请求进程级串行：资格校验、快照确认、复核落库、事件序号与
    # 「提交→发布」全程持锁
    with writer_lock():
        db.expire_all()
        response, event = create_result_review(db, match_id, payload)
        # 幂等重放（event 为 None）没有任何写入，直接返回首次结果
        if event is not None:
            commit_and_publish(db, [event])
    return response


@router.post(
    "/result-reviews/{review_id}/decisions",
    response_model=ResultReviewResponse,
)
def decide_review(
    review_id: int,
    payload: ResultReviewDecisionRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    with writer_lock():
        # 重复决定 / 改票 / 关闭判定全程持锁：并发重复不会产生额外
        # 决定记录，关闭时比分更正与关闭事件严格同事务
        review_response, event, created = decide_result_review(
            db, review_id, payload.participant_id, payload.vote
        )
        # 首次决定 201；重复决定或改票 200
        response.status_code = 201 if created else 200
        if event is not None:
            # 关闭（双方同意更正或任一驳回）：比分更正、状态与关闭
            # 事件同一事务提交
            commit_and_publish(db, [event])
        else:
            # 复核仍待处理：决定行需要持久化，但不产生事件
            db.commit()
        return review_response


@router.get(
    "/result-reviews/{review_id}",
    response_model=ResultReviewResponse,
)
def get_review(review_id: int, db: Session = Depends(get_db)):
    review = get_review_or_404(db, review_id)
    return serialize_review(db, review)


@router.get(
    "/tournaments/{tournament_id}/result-reviews",
    response_model=list[ResultReviewResponse],
)
def list_reviews(tournament_id: int, db: Session = Depends(get_db)):
    get_tournament_or_404(db, tournament_id)
    reviews = list_reviews_for_tournament(db, tournament_id)
    return [serialize_review(db, r) for r in reviews]
