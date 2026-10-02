"""晋级名单接口。

- POST /tournaments/{id}/promotions：发布一份晋级名单（赛事内版本递增）。
  请求携带幂等键 request_id、乐观快照 expected_event_seq、资格轮次
  qualifying_round_no 与晋级名额 slots；仅允许在所有已生成轮次结束且
  没有待处理赛果复核时发布。同一事务内重算排名积分与 Buchholz、冻结
  前 N 名席位与候补顺序，并写入 promotion.published 事件。
- POST /promotion-lists/{list_id}/forfeits：对已冻结名单标记一位当前
  有效晋级者放弃；同一事务内把最前候补提升为该席位（无候补则空席），
  并写入 promotion.substituted 事件。
- GET /tournaments/{id}/promotions：赛事全部版本（每席位原始/当前
  参赛者、状态与冻结依据、候补顺序）。
- GET /tournaments/{id}/promotions/{version}：指定版本。

重复 request_id（参数一致）原样返回首次结果，不产生新写入与事件；
同键不同参数返回 409。轮次未结束、有待处理复核、名额非法、快照冲突、
非当前有效晋级者一律 409 且不落库。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..events.service import commit_and_publish
from ..locking import writer_lock
from ..models import PromotionList
from ..schemas import (
    PromotionForfeitRequest,
    PromotionForfeitResponse,
    PromotionListResponse,
    PromotionPublishRequest,
)
from ..services.promotions import (
    forfeit_promotion_seat,
    get_list_by_version,
    list_lists,
    publish_promotion_list,
    serialize_list,
)
from .tournaments import get_tournament_or_404

router = APIRouter()


@router.post(
    "/tournaments/{tournament_id}/promotions",
    response_model=PromotionListResponse,
    status_code=201,
)
def publish(
    tournament_id: int,
    payload: PromotionPublishRequest,
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)
    # 写请求进程级串行：资格校验、快照确认、冻结落库、事件序号与
    # 「提交→发布」全程持锁
    with writer_lock():
        db.expire_all()
        response, event = publish_promotion_list(db, tournament_id, payload)
        # 幂等重放（event 为 None）没有任何写入，直接返回首次结果
        if event is not None:
            commit_and_publish(db, [event])
    return response


@router.post(
    "/promotion-lists/{list_id}/forfeits",
    response_model=PromotionForfeitResponse,
    status_code=201,
)
def forfeit(
    list_id: int,
    payload: PromotionForfeitRequest,
    db: Session = Depends(get_db),
):
    # 存在性先在锁外给出 404；锁内会以最新状态重新读取并校验
    if db.get(PromotionList, list_id) is None:
        raise HTTPException(status_code=404, detail="晋级名单不存在")

    with writer_lock():
        db.expire_all()
        response, event = forfeit_promotion_seat(db, list_id, payload)
        # 幂等重放（event 为 None）没有任何写入，直接返回首次结果
        if event is not None:
            commit_and_publish(db, [event])
    return response


@router.get(
    "/tournaments/{tournament_id}/promotions",
    response_model=list[PromotionListResponse],
)
def list_promotions(tournament_id: int, db: Session = Depends(get_db)):
    get_tournament_or_404(db, tournament_id)
    return [serialize_list(db, p) for p in list_lists(db, tournament_id)]


@router.get(
    "/tournaments/{tournament_id}/promotions/{version}",
    response_model=PromotionListResponse,
)
def get_promotion(
    tournament_id: int, version: int, db: Session = Depends(get_db)
):
    get_tournament_or_404(db, tournament_id)
    plist = get_list_by_version(db, tournament_id, version)
    if plist is None:
        raise HTTPException(status_code=404, detail="晋级名单版本不存在")
    return serialize_list(db, plist)
