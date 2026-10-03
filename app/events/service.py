"""赛事事件领域服务：事件类型、事务内原子写序号与分页查询。

事件与业务变更共用同一个 SQLAlchemy Session/事务：
- append_event() 通过 EventCounter 行上的原子 UPSERT
  （INSERT ... ON CONFLICT DO UPDATE ... RETURNING）在当前事务内
  分配赛事内下一个序号，不再「查询最大 seq 后插入」。并发事务在
  数据库层互斥排队：提交成功的序号唯一且连续（无重复、无空号），
  事务回滚则事件与计数器增量一并回滚；
- commit_and_publish() 统一提交，成功后按 seq 升序把快照扇出到
  事件总线，提交失败不广播任何事件。
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Event, EventCounter
from .bus import EventSnapshot, event_bus

# ---------- 事件类型 ----------

TOURNAMENT_CREATED = "tournament.created"
PARTICIPANT_REGISTERED = "participant.registered"
ROUND_GENERATED = "round.generated"
RESULT_SUBMITTED = "result.submitted"
ROUND_CLOSED = "round.closed"
STATION_PLAN_PUBLISHED = "station_plan.published"
ROUND_CLOCK_STARTED = "round_clock.started"
ROUND_CLOCK_PAUSED = "round_clock.paused"
ROUND_CLOCK_RESUMED = "round_clock.resumed"
ROUND_CLOCK_EXTENDED = "round_clock.extended"
ANNOUNCEMENT_PUBLISHED = "announcement.published"
ANNOUNCEMENT_ACKNOWLEDGED = "announcement.acknowledged"
RESULT_REVIEW_CREATED = "result_review.created"
RESULT_REVIEW_CLOSED = "result_review.closed"
PENALTY_RECORDED = "penalty.recorded"
PENALTY_REVOKED = "penalty.revoked"
CHECKIN_SNAPSHOT_CHANGED = "checkin.snapshot_changed"
CHECKIN_SNAPSHOT_CONFIRMED = "checkin.snapshot_confirmed"
PROMOTION_PUBLISHED = "promotion.published"
PROMOTION_SUBSTITUTED = "promotion.substituted"
TOURNAMENT_PAUSED = "tournament.paused"
TOURNAMENT_RESUMED = "tournament.resumed"
TOURNAMENT_ARCHIVED = "tournament.archived"


def _allocate_next_seq(db: Session, tournament_id: int) -> int:
    """在当前事务内原子分配赛事内下一个事件序号。

    依赖 events 表 (tournament_id, seq) 唯一约束作为最后防线。
    """
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        # 其他后端：计数器读改写（进程内 writer_lock 已使同一赛事的
        # 写请求串行；若在多进程下使用其他后端，由唯一约束兜底）。
        counter = db.get(EventCounter, tournament_id)
        if counter is None:
            next_seq = 1
            db.add(EventCounter(tournament_id=tournament_id, last_seq=1))
        else:
            next_seq = counter.last_seq + 1
            counter.last_seq = next_seq
        db.flush()
        return next_seq

    # 原子 UPSERT RETURNING：
    #   首次写入            -> last_seq=1，分配 seq 1；
    #   已存在（并发冲突）  -> last_seq=last_seq+1，分配下一个序号。
    # 两个并发事务的写锁由数据库互斥裁决：后执行者阻塞等待前者
    # COMMIT/ROLLBACK（SQLite 见 busy_timeout），绝不读到相同值。
    stmt = (
        dialect_insert(EventCounter)
        .values(tournament_id=tournament_id, last_seq=1)
        .on_conflict_do_update(
            index_elements=[EventCounter.tournament_id],
            set_={"last_seq": EventCounter.last_seq + 1},
        )
        .returning(EventCounter.last_seq)
    )
    return int(db.execute(stmt).scalar_one())


def append_event(
    db: Session,
    tournament_id: int,
    type_: str,
    payload: dict,
    *,
    occurred_at: datetime | None = None,
) -> Event:
    """在当前事务内追加一条赛事事件，序号由计数器原子分配。

    调用方负责最终 commit；序号在 flush 后即可用。
    """
    next_seq = _allocate_next_seq(db, tournament_id)
    event = Event(
        tournament_id=tournament_id,
        seq=next_seq,
        type=type_,
        payload=payload,
        occurred_at=occurred_at or datetime.utcnow(),
    )
    db.add(event)
    db.flush()
    return event


def snapshot(event: Event) -> EventSnapshot:
    return EventSnapshot(
        tournament_id=event.tournament_id,
        seq=event.seq,
        type=event.type,
        payload=dict(event.payload),
        occurred_at=event.occurred_at,
    )


def commit_and_publish(db: Session, events: list[Event]) -> None:
    """提交事务；成功后按序号升序把事件实时扇出给订阅者。

    提交失败（如数据库错误）时事件与业务变更一同回滚，
    且不会向订阅者发布任何内容。调用方应在 writer_lock 内调用，
    使「提交顺序」与「发布顺序」严格一致。
    """
    ordered = sorted(events, key=lambda e: e.seq)
    snapshots = [snapshot(e) for e in ordered]
    db.commit()
    event_bus.publish(snapshots)


def fetch_events_after(
    db: Session,
    tournament_id: int,
    after_seq: int,
    limit: int,
) -> list[Event]:
    """按序号升序读取赛事内 seq > after_seq 的事件（最多 limit 条）。"""
    return list(
        db.scalars(
            select(Event)
            .where(Event.tournament_id == tournament_id, Event.seq > after_seq)
            .order_by(Event.seq.asc())
            .limit(limit)
        )
    )
