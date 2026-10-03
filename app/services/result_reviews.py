"""已提交赛果的可审计复核流程。

- create_result_review：为一场对局创建待处理复核。仅允许非轮空、
  已提交赛果且尚未生成下一轮的对局（赛事存在该对局之后的轮次时
  即视为"已生成下一轮"）；轮空/未提交/已有待处理复核均拒绝。
  创建时冻结原比分，并在同一写事务内确认赛事事件序号快照
  （expected_event_seq）未变化，写入 result_review.created 事件。
  request_id 提供幂等重放，同键不同参数返回 409。
- decide_result_review：对局双方分别同意/驳回。participant_id
  必须是本场选手；重复提交相同决定原样返回首次结果（不产生额外
  记录），pending 期间改票原地更新决定行。任一驳回即以 rejected
  关闭、比分不变；双方同意时在同一写事务内更正比分并以 approved
  关闭，随后写入 result_review.closed 事件。
- 排名（/standings）始终由当前比分实时计算，复核经双方同意更正
  比分后，可见排名在该事务提交时即完成重算，无需额外写表。

注意：本模块的写函数只在调用方事务内写入并 flush，不提交；
复核行、决定行、幂等记录、比分更正与 result_review.* 事件由路由
层在同一事务中统一提交。所有调用均处于进程级 writer_lock 内，
"改票/同一复核的并发重复"因此不会产生额外记录。
"""
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..events.service import (
    RESULT_REVIEW_CLOSED,
    RESULT_REVIEW_CREATED,
    append_event,
)
from ..models import (
    Event,
    Match,
    Participant,
    ResultReview,
    ResultReviewAction,
    ResultReviewDecision,
    Round,
)
from ..schemas import (
    ResultReviewCreate,
    ResultReviewDecisionResponse,
    ResultReviewResponse,
)
from .announcements import current_event_seq
from .scoring import half_to_float
from .swiss import WIN

# 允许的对局得分（半分单位 0 / 1 / 2）
VALID_HALF_SCORES = {0, 1, 2}
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"


def has_pending_review_for_tournament(
    db: Session, tournament_id: int
) -> bool:
    """赛事是否存在待处理复核（存在时禁止生成下一轮）。"""
    return (
        db.query(ResultReview.id)
        .join(Match, Match.id == ResultReview.match_id)
        .join(Round, Round.id == Match.round_id)
        .filter(Round.tournament_id == tournament_id, ResultReview.status == PENDING)
        .first()
        is not None
    )


def pending_review_for_match(db: Session, match_id: int) -> ResultReview | None:
    return (
        db.query(ResultReview)
        .filter(ResultReview.match_id == match_id, ResultReview.status == PENDING)
        .first()
    )


def _params_fingerprint(req: ResultReviewCreate) -> dict:
    return {
        "expected_event_seq": req.expected_event_seq,
        "score_white": req.score_white,
        "score_black": req.score_black,
        "reason": req.reason,
    }


def _find_replay(
    db: Session, match_id: int, request_id: str, fingerprint: dict
) -> ResultReviewResponse | None:
    """同 request_id 的已成功请求：参数一致返回首次响应，否则 409。"""
    record = (
        db.query(ResultReviewAction)
        .filter(
            ResultReviewAction.match_id == match_id,
            ResultReviewAction.request_id == request_id,
        )
        .first()
    )
    if record is None:
        return None
    if record.params == fingerprint:
        return ResultReviewResponse(**record.response)
    raise HTTPException(
        status_code=409,
        detail="request_id 已被用于该对局的其他复核请求，请更换 request_id",
    )


def get_review_or_404(db: Session, review_id: int) -> ResultReview:
    review = db.get(ResultReview, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="复核不存在")
    return review


def serialize_review(db: Session, review: ResultReview) -> ResultReviewResponse:
    """组装复核详情：冻结原比分、提议、双方决定与最终状态。"""
    match = db.get(Match, review.match_id)
    round_obj = db.get(Round, match.round_id)
    participants = {
        p.id: p
        for p in db.query(Participant)
        .filter(
            Participant.id.in_(
                [match.player_white_id, match.player_black_id]
            )
        )
        .all()
    }

    decisions = [
        ResultReviewDecisionResponse(
            id=d.id,
            participant_id=d.participant_id,
            number=participants[d.participant_id].number,
            name=participants[d.participant_id].name,
            vote=d.vote,
            decided_at=d.created_at,
            updated_at=d.updated_at,
        )
        for d in review.decisions
    ]

    return ResultReviewResponse(
        id=review.id,
        match_id=match.id,
        tournament_id=round_obj.tournament_id,
        round_id=round_obj.id,
        round_no=round_obj.round_no,
        player_white=participants[match.player_white_id],
        player_black=participants[match.player_black_id],
        expected_event_seq=review.expected_event_seq,
        original_score_white=half_to_float(review.original_score_white),
        original_score_black=half_to_float(review.original_score_black),
        proposed_score_white=half_to_float(review.proposed_score_white),
        proposed_score_black=half_to_float(review.proposed_score_black),
        reason=review.reason,
        status=review.status,
        decisions=decisions,
        created_at=review.created_at,
        closed_at=review.closed_at,
    )


def create_result_review(
    db: Session, match_id: int, req: ResultReviewCreate
) -> tuple[ResultReviewResponse, Event | None]:
    """创建待处理复核（flush，不提交），返回（响应, 事件）。

    幂等重放时返回（首次响应, None），不产生任何写入；资格不符或
    事件序号快照冲突抛 409，且在任何写入之前抛出（不落库）。
    """
    # 写锁可能经过等待才取得：以锁内已提交的最新状态重新校验
    match = db.get(Match, match_id)
    if match is None:
        raise HTTPException(status_code=404, detail="对局不存在")

    fingerprint = _params_fingerprint(req)
    # 幂等判定先于业务校验：重试已成功请求必须返回原结果
    replay = _find_replay(db, match_id, req.request_id, fingerprint)
    if replay is not None:
        return replay, None

    round_obj = db.get(Round, match.round_id)

    # 维护/归档期间拒绝新的复核写入（重放已在上方返回首次结果）
    from .lifecycle import ensure_tournament_writable

    ensure_tournament_writable(db, round_obj.tournament_id)

    if match.player_black_id is None:
        raise HTTPException(status_code=409, detail="轮空对局不能申请复核")
    if match.score_white is None or match.score_black is None:
        raise HTTPException(status_code=409, detail="该对局尚未提交赛果，不能复核")
    if pending_review_for_match(db, match_id) is not None:
        raise HTTPException(
            status_code=409, detail="该对局已存在待处理复核，不能重复申请"
        )
    # 已生成下一轮（该对局所属轮次之后还有轮次）：比分已影响后续编排
    later_round = (
        db.query(Round.id)
        .filter(
            Round.tournament_id == round_obj.tournament_id,
            Round.round_no > round_obj.round_no,
        )
        .first()
    )
    if later_round is not None:
        raise HTTPException(
            status_code=409,
            detail="该对局之后已生成下一轮，不能再申请赛果复核",
        )

    sw = round(req.score_white * 2)
    sb = round(req.score_black * 2)
    if sw not in VALID_HALF_SCORES or sb not in VALID_HALF_SCORES:
        raise HTTPException(status_code=422, detail="每场得分只能是 0、0.5 或 1")
    if sw + sb != WIN:
        raise HTTPException(status_code=422, detail="双方得分之和必须为 1")

    # 先完成全部资格校验，再在同一写事务内确认事件序号快照未变化
    actual_seq = current_event_seq(db, round_obj.tournament_id)
    if req.expected_event_seq != actual_seq:
        raise HTTPException(
            status_code=409,
            detail=(
                f"赛事快照已变化：当前最大事件序号为 {actual_seq}，"
                f"请求期望 {req.expected_event_seq}"
            ),
        )

    review = ResultReview(
        match_id=match_id,
        expected_event_seq=req.expected_event_seq,
        original_score_white=match.score_white,
        original_score_black=match.score_black,
        proposed_score_white=sw,
        proposed_score_black=sb,
        reason=req.reason,
        status=PENDING,
    )
    db.add(review)
    db.flush()  # 取得 review.id，供幂等记录与事件使用

    event = append_event(
        db,
        round_obj.tournament_id,
        RESULT_REVIEW_CREATED,
        {
            "review_id": review.id,
            "match_id": match.id,
            "round_id": round_obj.id,
            "round_no": round_obj.round_no,
            "player_white_id": match.player_white_id,
            "player_black_id": match.player_black_id,
            "original_score_white": half_to_float(match.score_white),
            "original_score_black": half_to_float(match.score_black),
            "proposed_score_white": half_to_float(sw),
            "proposed_score_black": half_to_float(sb),
            "expected_event_seq": req.expected_event_seq,
        },
    )

    response = serialize_review(db, review)
    db.add(
        ResultReviewAction(
            match_id=match_id,
            review_id=review.id,
            request_id=req.request_id,
            params=fingerprint,
            response=response.model_dump(mode="json"),
        )
    )
    db.flush()
    return response, event


def decide_result_review(
    db: Session,
    review_id: int,
    participant_id: int,
    vote: str,
) -> tuple[ResultReviewResponse, Event | None, bool]:
    """记录一方选手的决定（flush，不提交）。

    返回（响应, 事件, created）：
    - created=True：首次决定（调用方返回 201）；
    - created=False：重复提交相同决定（created=False、event=None，
      不产生任何写入，调用方返回 200）；pending 期间改票原地更新
      （created=False、event 通常为 None，若因此关闭则附带关闭事件）。
    """
    review = get_review_or_404(db, review_id)
    match = db.get(Match, review.match_id)
    round_obj = db.get(Round, match.round_id)

    player_ids = {match.player_white_id, match.player_black_id}
    participant = (
        db.query(Participant)
        .filter(
            Participant.id == participant_id,
            Participant.tournament_id == round_obj.tournament_id,
        )
        .first()
    )
    if participant is None:
        raise HTTPException(status_code=404, detail="参赛者不存在")
    if participant_id not in player_ids:
        raise HTTPException(
            status_code=409, detail="该参赛者不是本场对局选手，不能参与复核决定"
        )

    # 维护/归档期间不允许改变复核状态（已关闭复核的重复决定在
    # 下方原样返回，属于查询语义，不产生写入）
    from .lifecycle import ensure_tournament_writable

    ensure_tournament_writable(db, round_obj.tournament_id)

    decision = next(
        (d for d in review.decisions if d.participant_id == participant_id),
        None,
    )

    # 复核已关闭：已有决定的选手重复提交原样返回其决定，不产生写入
    if review.status != PENDING:
        if decision is not None:
            return serialize_review(db, review), None, False
        raise HTTPException(status_code=409, detail="该复核已关闭，不能再作出决定")

    created = decision is None
    if decision is not None and decision.vote == vote:
        # 相同决定重复提交：原样返回当前状态，不改票、不产生记录
        return serialize_review(db, review), None, False

    now = datetime.utcnow()
    if decision is None:
        # 经关系集合追加：back_populates 会同步 review 与外键，
        # review.decisions 在 flush 前后都反映最新状态（直接设置
        # review_id 不会可靠刷新已加载的内存集合）
        decision = ResultReviewDecision(
            participant_id=participant_id,
            vote=vote,
        )
        review.decisions.append(decision)
    else:
        # pending 期间改票：原地更新，不产生额外决定记录
        decision.vote = vote
        decision.updated_at = now
    db.flush()

    event: Event | None = None
    # 任一驳回即关闭（比分不变）；双方同意时同一事务内更正比分后关闭。
    # 重新读取决定集合：改票也可能把复核带入关闭状态。
    votes = {d.participant_id: d.vote for d in review.decisions}
    rejected_by = next(
        (pid for pid, v in votes.items() if v == "reject"), None
    )
    if rejected_by is not None:
        review.status = REJECTED
        review.closed_at = now
        db.flush()
        event = append_event(
            db,
            round_obj.tournament_id,
            RESULT_REVIEW_CLOSED,
            {
                "review_id": review.id,
                "match_id": match.id,
                "round_id": round_obj.id,
                "round_no": round_obj.round_no,
                "outcome": REJECTED,
                "rejected_by_participant_id": rejected_by,
                "score_white": half_to_float(review.original_score_white),
                "score_black": half_to_float(review.original_score_black),
                "score_changed": False,
            },
        )
    elif len(votes) == 2 and all(v == "approve" for v in votes.values()):
        # 双方同意：比分更正确冻结的拟更正值；排名由当前比分实时
        # 计算，提交后可见排名即已重算（同一写事务）。
        match.score_white = review.proposed_score_white
        match.score_black = review.proposed_score_black
        review.status = APPROVED
        review.closed_at = now
        db.flush()
        event = append_event(
            db,
            round_obj.tournament_id,
            RESULT_REVIEW_CLOSED,
            {
                "review_id": review.id,
                "match_id": match.id,
                "round_id": round_obj.id,
                "round_no": round_obj.round_no,
                "outcome": APPROVED,
                "original_score_white": half_to_float(review.original_score_white),
                "original_score_black": half_to_float(review.original_score_black),
                "score_white": half_to_float(review.proposed_score_white),
                "score_black": half_to_float(review.proposed_score_black),
                "score_changed": (
                    review.proposed_score_white != review.original_score_white
                    or review.proposed_score_black != review.original_score_black
                ),
            },
        )

    return serialize_review(db, review), event, created


def list_reviews_for_tournament(
    db: Session, tournament_id: int
) -> list[ResultReview]:
    """赛事下的全部复核（按创建顺序）。"""
    return list(
        db.query(ResultReview)
        .join(Match, Match.id == ResultReview.match_id)
        .join(Round, Round.id == Match.round_id)
        .filter(Round.tournament_id == tournament_id)
        .order_by(ResultReview.id)
        .all()
    )
