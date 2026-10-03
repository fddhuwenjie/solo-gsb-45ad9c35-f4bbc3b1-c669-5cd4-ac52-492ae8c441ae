"""定向赛务公告与确认回执接口。

- POST /tournaments/{id}/announcements：发布新公告。请求携带幂等键
  request_id 与乐观快照 expected_event_seq；收件规则三类选择器
  （参赛者 / 当前排名区间 / 某版工位方案的工位号）取并集后应用
  排除名单。规则无效或事件序号快照已变化时 409 且不落库；成功
  时在同一写事务内确认序号、冻结去重收件人并写入
  announcement.published 事件。
- POST /announcements/{id}/revisions：公告不可改写，本端点生成
  新版本并重新冻结收件人；旧版本与旧版回执保留且不沿用。
- POST /announcements/{id}/revisions/{version}/acknowledgements：
  仅该版收件人可确认；同一 participant_id 重复提交返回原回执。
- GET 系列：公告（含最新版）、版本列表/详情、已确认与未确认名单。
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..events.service import commit_and_publish
from ..locking import writer_lock
from ..models import Announcement
from ..schemas import (
    AnnouncementAckListResponse,
    AnnouncementAckRequest,
    AnnouncementAckResponse,
    AnnouncementCreate,
    AnnouncementResponse,
    AnnouncementRevisionCreate,
    AnnouncementRevisionResponse,
)
from ..services.announcements import (
    acknowledge,
    get_revision_or_404,
    list_acknowledgements,
    publish_announcement,
    revise_announcement,
    serialize_announcement,
    serialize_revision,
)
from ..services.lifecycle import ensure_tournament_writable
from .tournaments import get_tournament_or_404

router = APIRouter()


def _get_announcement_or_404(
    db: Session, announcement_id: int
) -> Announcement:
    # 公告 id 全局唯一；不存在即 404（发布与赛事内列表另校验赛事归属）
    announcement = db.get(Announcement, announcement_id)
    if announcement is None:
        raise HTTPException(status_code=404, detail="公告不存在")
    return announcement


@router.post(
    "/tournaments/{tournament_id}/announcements",
    response_model=AnnouncementResponse,
    status_code=201,
)
def create_announcement(
    tournament_id: int,
    payload: AnnouncementCreate,
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)
    # 写请求进程级串行：规则解析、序号快照确认、冻结收件人、事件
    # 序号与「提交→发布」全程持锁
    with writer_lock():
        ensure_tournament_writable(db, tournament_id)
        response, event = publish_announcement(db, tournament_id, payload)
        # 幂等重放（event 为 None）没有任何写入，直接返回首次结果
        if event is not None:
            commit_and_publish(db, [event])
    return response


@router.get(
    "/tournaments/{tournament_id}/announcements",
    response_model=list[AnnouncementResponse],
)
def list_announcements(tournament_id: int, db: Session = Depends(get_db)):
    get_tournament_or_404(db, tournament_id)
    announcements = (
        db.query(Announcement)
        .filter(Announcement.tournament_id == tournament_id)
        .order_by(Announcement.id)
        .all()
    )
    return [serialize_announcement(a) for a in announcements]


@router.get(
    "/announcements/{announcement_id}",
    response_model=AnnouncementResponse,
)
def get_announcement(announcement_id: int, db: Session = Depends(get_db)):
    announcement = _get_announcement_or_404(db, announcement_id)
    return serialize_announcement(announcement)


@router.post(
    "/announcements/{announcement_id}/revisions",
    response_model=AnnouncementResponse,
    status_code=201,
)
def create_revision(
    announcement_id: int,
    payload: AnnouncementRevisionCreate,
    db: Session = Depends(get_db),
):
    announcement = _get_announcement_or_404(db, announcement_id)
    with writer_lock():
        ensure_tournament_writable(db, announcement.tournament_id)
        response, event = revise_announcement(
            db, announcement.tournament_id, announcement, payload
        )
        if event is not None:
            commit_and_publish(db, [event])
    return response


@router.get(
    "/announcements/{announcement_id}/revisions",
    response_model=list[AnnouncementRevisionResponse],
)
def list_revisions(announcement_id: int, db: Session = Depends(get_db)):
    announcement = _get_announcement_or_404(db, announcement_id)
    return [serialize_revision(r) for r in announcement.revisions]


@router.get(
    "/announcements/{announcement_id}/revisions/{version}",
    response_model=AnnouncementRevisionResponse,
)
def get_revision(
    announcement_id: int, version: int, db: Session = Depends(get_db)
):
    announcement = _get_announcement_or_404(db, announcement_id)
    return serialize_revision(get_revision_or_404(announcement, version))


@router.post(
    "/announcements/{announcement_id}/revisions/{version}/acknowledgements",
    response_model=AnnouncementAckResponse,
)
def create_acknowledgement(
    announcement_id: int,
    version: int,
    payload: AnnouncementAckRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    announcement = _get_announcement_or_404(db, announcement_id)
    revision = get_revision_or_404(announcement, version)
    with writer_lock():
        ensure_tournament_writable(db, announcement.tournament_id)
        # 收件人判定以冻结快照为准；重复提交返回原回执（200，无写入）
        receipt, event = acknowledge(
            db, announcement.tournament_id, revision, payload.participant_id
        )
        if event is not None:
            commit_and_publish(db, [event])
            response.status_code = 201
    return receipt


@router.get(
    "/announcements/{announcement_id}/revisions/{version}/acknowledgements",
    response_model=AnnouncementAckListResponse,
)
def get_acknowledgements(
    announcement_id: int, version: int, db: Session = Depends(get_db)
):
    announcement = _get_announcement_or_404(db, announcement_id)
    revision = get_revision_or_404(announcement, version)
    return list_acknowledgements(db, revision)
