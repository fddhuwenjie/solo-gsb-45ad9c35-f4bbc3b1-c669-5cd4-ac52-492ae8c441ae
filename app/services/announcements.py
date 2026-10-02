"""定向赛务公告：收件规则解析、冻结、版本化与确认回执。

收件规则（AnnouncementRules）由三类选择器取并集，再应用排除名单：

- participant_ids：直接指定参赛者 id；
- rank_ranges：按当前排名（积分 → Buchholz → 编号，实时计入
  进行中轮次）的闭区间指定；
- station_plans：指定「某轮某版工位方案」中某些工位号上对局的
  双方选手。

所有引用（参赛者、轮次/方案版本、工位号）必须有效且属于当前
赛事，任一无效即 409 且不落任何数据。解析、去重后的收件人在
发布/修订的同一写事务内冻结到 announcement_recipients，此后
排名或工位方案变化不影响该版本。

乐观快照：发布/修订须在写事务内确认赛事当前最大事件序号等于
expected_event_seq，否则 409 不落库。

公告本体不可改写：修订（POST .../revisions）只生成新版本并
重新冻结收件人；旧版本与其回执原样保留，回执不沿用。

注意：本模块的写函数只在调用方事务内写入并 flush，不提交；
公告/版本/收件人/幂等记录与 announcement.* 事件由路由层在
同一事务中统一提交。
"""
from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..events.service import (
    ANNOUNCEMENT_ACKNOWLEDGED,
    ANNOUNCEMENT_PUBLISHED,
    append_event,
)
from ..models import (
    Announcement,
    AnnouncementAcknowledgement,
    AnnouncementAction,
    AnnouncementRecipient,
    AnnouncementRevision,
    Event,
    Match,
    Participant,
    Round,
    StationPlan,
    StationPlanAssignment,
)
from ..schemas import (
    AnnouncementAckResponse,
    AnnouncementAckListResponse,
    AnnouncementCreate,
    AnnouncementRecipientResponse,
    AnnouncementResponse,
    AnnouncementRevisionCreate,
    AnnouncementRevisionResponse,
    AnnouncementRules,
    AnnouncementRulesResponse,
)
from .scoring import compute_records


def current_event_seq(db: Session, tournament_id: int) -> int:
    """赛事当前已分配的最大事件序号（无事件时为 0）。"""
    from ..models import EventCounter

    counter = db.get(EventCounter, tournament_id)
    return counter.last_seq if counter is not None else 0


def _rules_fingerprint(rules: AnnouncementRules) -> dict:
    """规则的稳定指纹：各列表已在 schema 层去重排序，dump 结果稳定。"""
    return rules.model_dump()


def _participants_by_id(
    db: Session, tournament_id: int, ids: list[int]
) -> dict[int, Participant]:
    if not ids:
        return {}
    rows = (
        db.query(Participant)
        .filter(
            Participant.tournament_id == tournament_id,
            Participant.id.in_(ids),
        )
        .all()
    )
    return {p.id: p for p in rows}


def _validate_participant_refs(
    db: Session,
    tournament_id: int,
    ids: list[int],
    problems: list[str],
    *,
    label: str,
) -> dict[int, Participant]:
    """校验一批参赛者 id 全部属于本赛事；问题累积进 problems。"""
    found = _participants_by_id(db, tournament_id, ids)
    for pid in ids:
        if pid not in found:
            problems.append(f"{label}中的参赛者 {pid} 不存在或不属于本赛事")
    return found


def _rank_recipients(
    db: Session,
    tournament_id: int,
    rules: AnnouncementRules,
    problems: list[str],
) -> set[int]:
    """排名区间选择器：区间越界记为规则无效。"""
    if not rules.rank_ranges:
        return set()
    records = compute_records(db, tournament_id, include_open=True)
    total = len(records)
    result: set[int] = set()
    for rng in rules.rank_ranges:
        if rng.rank_to > total:
            problems.append(
                f"排名区间 [{rng.rank_from}, {rng.rank_to}] 超出当前参赛人数"
                f"（共 {total} 人）"
            )
            continue
        result.update(
            records[i - 1].participant_id
            for i in range(rng.rank_from, rng.rank_to + 1)
        )
    return result


def _station_plan_recipients(
    db: Session,
    tournament_id: int,
    rules: AnnouncementRules,
    problems: list[str],
) -> set[int]:
    """工位方案选择器：轮次/版本/工位号任一无效都记为规则无效。"""
    result: set[int] = set()
    for ref in rules.station_plans:
        round_obj = (
            db.query(Round)
            .filter(
                Round.tournament_id == tournament_id,
                Round.round_no == ref.round_no,
            )
            .first()
        )
        if round_obj is None:
            problems.append(f"第 {ref.round_no} 轮不存在，不能作为收件规则")
            continue
        plan = (
            db.query(StationPlan)
            .filter(
                StationPlan.round_id == round_obj.id,
                StationPlan.version == ref.version,
            )
            .first()
        )
        if plan is None:
            problems.append(
                f"第 {ref.round_no} 轮不存在第 {ref.version} 版工位方案"
            )
            continue
        assignments = (
            db.query(StationPlanAssignment)
            .filter(StationPlanAssignment.plan_id == plan.id)
            .all()
        )
        station_players: dict[int, set[int]] = {}
        for a in assignments:
            match = db.get(Match, a.match_id)
            if match is None:
                continue
            players = {
                pid
                for pid in (match.player_white_id, match.player_black_id)
                if pid is not None
            }
            station_players.setdefault(a.station_no, set()).update(players)
        missing = [no for no in ref.station_nos if no not in station_players]
        if missing:
            problems.append(
                f"第 {ref.round_no} 轮第 {ref.version} 版工位方案中没有工位 "
                + "、".join(str(no) for no in missing)
            )
            continue
        for no in ref.station_nos:
            result.update(station_players[no])
    return result


def resolve_recipients(
    db: Session, tournament_id: int, rules: AnnouncementRules
) -> list[Participant]:
    """校验并解析收件规则，返回去重后按编号升序的收件人列表。

    规则无效（引用了不属于本赛事的参赛者/轮次/方案版本/工位号、
    排名区间越界）或最终收件人为空时抛 409；调用方此时尚未写入。
    """
    problems: list[str] = []

    direct = _validate_participant_refs(
        db, tournament_id, rules.participant_ids, problems, label="收件名单"
    )
    excluded = _validate_participant_refs(
        db,
        tournament_id,
        rules.exclude_participant_ids,
        problems,
        label="排除名单",
    )
    by_rank = _rank_recipients(db, tournament_id, rules, problems)
    by_station = _station_plan_recipients(db, tournament_id, rules, problems)

    if problems:
        raise HTTPException(status_code=409, detail="；".join(problems))

    ids = set(direct) | by_rank | by_station
    ids -= set(excluded)
    if not ids:
        raise HTTPException(status_code=409, detail="收件规则解析后没有任何收件人")
    return (
        db.query(Participant)
        .filter(
            Participant.tournament_id == tournament_id,
            Participant.id.in_(ids),
        )
        .order_by(Participant.number)
        .all()
    )


def _assert_snapshot(db: Session, tournament_id: int, expected_seq: int) -> None:
    """同一写事务内确认事件序号未变化（乐观快照）。"""
    actual = current_event_seq(db, tournament_id)
    if expected_seq != actual:
        raise HTTPException(
            status_code=409,
            detail=(
                f"赛事快照已变化：当前最大事件序号为 {actual}，"
                f"请求期望 {expected_seq}"
            ),
        )


def _find_action(
    db: Session, tournament_id: int, request_id: str, fingerprint: dict
) -> AnnouncementResponse | None:
    """同 request_id 的已成功请求：参数一致返回首次响应，否则 409。"""
    record = (
        db.query(AnnouncementAction)
        .filter(
            AnnouncementAction.tournament_id == tournament_id,
            AnnouncementAction.request_id == request_id,
        )
        .first()
    )
    if record is None:
        return None
    if record.params == fingerprint:
        return AnnouncementResponse(**record.response)
    raise HTTPException(
        status_code=409,
        detail="request_id 已被用于其他公告请求，请更换 request_id",
    )


def serialize_revision(revision: AnnouncementRevision) -> AnnouncementRevisionResponse:
    return AnnouncementRevisionResponse(
        id=revision.id,
        announcement_id=revision.announcement_id,
        tournament_id=revision.announcement.tournament_id,
        version=revision.version,
        body=revision.body,
        rules=AnnouncementRulesResponse(**revision.rules),
        expected_event_seq=revision.expected_event_seq,
        recipient_count=len(revision.recipients),
        acknowledged_count=len(revision.acknowledgements),
        created_at=revision.created_at,
    )


def serialize_announcement(announcement: Announcement) -> AnnouncementResponse:
    latest = announcement.revisions[-1]
    return AnnouncementResponse(
        id=announcement.id,
        tournament_id=announcement.tournament_id,
        latest_version=latest.version,
        created_at=announcement.created_at,
        latest_revision=serialize_revision(latest),
    )


def _fingerprint(
    action: str, body: str, expected_seq: int, rules: AnnouncementRules, **extra
) -> dict:
    return {
        "action": action,
        "body": body,
        "expected_event_seq": expected_seq,
        "rules": _rules_fingerprint(rules),
        **extra,
    }


def _freeze_revision(
    db: Session,
    announcement: Announcement,
    version: int,
    body: str,
    expected_seq: int,
    rules: AnnouncementRules,
    recipients: list[Participant],
) -> AnnouncementRevision:
    revision = AnnouncementRevision(
        announcement_id=announcement.id,
        version=version,
        body=body,
        rules=_rules_fingerprint(rules),
        expected_event_seq=expected_seq,
    )
    db.add(revision)
    db.flush()  # 取得 revision.id，供收件人/回执关系使用
    for p in recipients:
        db.add(
            AnnouncementRecipient(
                revision_id=revision.id, participant_id=p.id
            )
        )
    db.flush()
    return revision


def publish_announcement(
    db: Session, tournament_id: int, req: AnnouncementCreate
) -> tuple[AnnouncementResponse, Event | None]:
    """发布新公告（flush，不提交），返回（响应, 事件）。

    幂等重放时返回（首次响应, None），不产生任何写入；规则无效或
    事件序号快照冲突抛 409，且在任何写入之前抛出（不落库）。
    """
    fingerprint = _fingerprint(
        "publish", req.body, req.expected_event_seq, req.rules
    )
    replay = _find_action(db, tournament_id, req.request_id, fingerprint)
    if replay is not None:
        return replay, None

    # 先校验规则（无效不落库），再在同一写事务内确认快照未变化
    recipients = resolve_recipients(db, tournament_id, req.rules)
    _assert_snapshot(db, tournament_id, req.expected_event_seq)

    announcement = Announcement(tournament_id=tournament_id)
    db.add(announcement)
    db.flush()  # 取得 announcement.id
    revision = _freeze_revision(
        db,
        announcement,
        1,
        req.body,
        req.expected_event_seq,
        req.rules,
        recipients,
    )

    event = append_event(
        db,
        tournament_id,
        ANNOUNCEMENT_PUBLISHED,
        {
            "announcement_id": announcement.id,
            "version": 1,
            "participant_ids": [p.id for p in recipients],
            "expected_event_seq": req.expected_event_seq,
        },
    )

    response = serialize_announcement(announcement)
    db.add(
        AnnouncementAction(
            tournament_id=tournament_id,
            announcement_id=announcement.id,
            revision_id=revision.id,
            request_id=req.request_id,
            action="publish",
            params=fingerprint,
            response=response.model_dump(mode="json"),
        )
    )
    db.flush()
    return response, event


def revise_announcement(
    db: Session,
    tournament_id: int,
    announcement: Announcement,
    req: AnnouncementRevisionCreate,
) -> tuple[AnnouncementResponse, Event | None]:
    """生成公告新版本并重新冻结收件人（flush，不提交）。"""
    current_version = announcement.revisions[-1].version
    fingerprint = _fingerprint(
        "revise",
        req.body,
        req.expected_event_seq,
        req.rules,
        announcement_id=announcement.id,
    )
    replay = _find_action(db, tournament_id, req.request_id, fingerprint)
    if replay is not None:
        return replay, None

    # 规则校验与快照确认同事务完成，任一不过则不落库
    recipients = resolve_recipients(db, tournament_id, req.rules)
    _assert_snapshot(db, tournament_id, req.expected_event_seq)

    revision = _freeze_revision(
        db,
        announcement,
        current_version + 1,
        req.body,
        req.expected_event_seq,
        req.rules,
        recipients,
    )

    event = append_event(
        db,
        tournament_id,
        ANNOUNCEMENT_PUBLISHED,
        {
            "announcement_id": announcement.id,
            "version": revision.version,
            "participant_ids": [p.id for p in recipients],
            "expected_event_seq": req.expected_event_seq,
        },
    )

    db.refresh(announcement)
    response = serialize_announcement(announcement)
    db.add(
        AnnouncementAction(
            tournament_id=tournament_id,
            announcement_id=announcement.id,
            revision_id=revision.id,
            request_id=req.request_id,
            action="revise",
            params=fingerprint,
            response=response.model_dump(mode="json"),
        )
    )
    db.flush()
    return response, event


def get_revision_or_404(
    announcement: Announcement, version: int
) -> AnnouncementRevision:
    revision = next(
        (r for r in announcement.revisions if r.version == version), None
    )
    if revision is None:
        raise HTTPException(status_code=404, detail="该版本的公告不存在")
    return revision


def acknowledge(
    db: Session,
    tournament_id: int,
    revision: AnnouncementRevision,
    participant_id: int,
) -> tuple[AnnouncementAckResponse, Event | None]:
    """该版收件人确认回执（flush，不提交）。

    - 参赛者不存在/不属于本赛事：404；
    - 参赛者不是该版冻结收件人：409；
    - 同一 participant_id 重复提交：原样返回首次回执（event 为
      None，不重复写库、不重复产生事件）。
    """
    participant = (
        db.query(Participant)
        .filter(
            Participant.id == participant_id,
            Participant.tournament_id == tournament_id,
        )
        .first()
    )
    if participant is None:
        raise HTTPException(status_code=404, detail="参赛者不存在")

    is_recipient = (
        db.query(AnnouncementRecipient.id)
        .filter(
            AnnouncementRecipient.revision_id == revision.id,
            AnnouncementRecipient.participant_id == participant_id,
        )
        .first()
        is not None
    )
    if not is_recipient:
        raise HTTPException(
            status_code=409,
            detail="该参赛者不是此版本公告的收件人，不能确认回执",
        )

    existing = (
        db.query(AnnouncementAcknowledgement)
        .filter(
            AnnouncementAcknowledgement.revision_id == revision.id,
            AnnouncementAcknowledgement.participant_id == participant_id,
        )
        .first()
    )
    if existing is not None:
        return (
            AnnouncementAckResponse(
                announcement_id=revision.announcement_id,
                version=revision.version,
                participant_id=participant.id,
                number=participant.number,
                name=participant.name,
                acknowledged_at=existing.created_at,
            ),
            None,
        )

    ack = AnnouncementAcknowledgement(
        revision_id=revision.id, participant_id=participant_id
    )
    db.add(ack)
    db.flush()
    receipt = AnnouncementAckResponse(
        announcement_id=revision.announcement_id,
        version=revision.version,
        participant_id=participant.id,
        number=participant.number,
        name=participant.name,
        acknowledged_at=ack.created_at,
    )
    event = append_event(
        db,
        tournament_id,
        ANNOUNCEMENT_ACKNOWLEDGED,
        {
            "announcement_id": revision.announcement_id,
            "version": revision.version,
            "participant_id": participant_id,
        },
    )
    return receipt, event


def list_acknowledgements(
    db: Session, revision: AnnouncementRevision
) -> AnnouncementAckListResponse:
    """返回该版收件人的已确认 / 未确认名单（按参赛者编号升序）。"""
    acked_ids = {a.participant_id for a in revision.acknowledgements}
    participants = (
        db.query(Participant)
        .filter(
            Participant.id.in_(
                [r.participant_id for r in revision.recipients]
            )
        )
        .order_by(Participant.number)
        .all()
    )

    def _to_recipient(p: Participant) -> AnnouncementRecipientResponse:
        return AnnouncementRecipientResponse(
            participant_id=p.id, number=p.number, name=p.name
        )

    acknowledged = [p for p in participants if p.id in acked_ids]
    pending = [p for p in participants if p.id not in acked_ids]
    return AnnouncementAckListResponse(
        announcement_id=revision.announcement_id,
        version=revision.version,
        acknowledged=[_to_recipient(p) for p in acknowledged],
        pending=[_to_recipient(p) for p in pending],
    )
