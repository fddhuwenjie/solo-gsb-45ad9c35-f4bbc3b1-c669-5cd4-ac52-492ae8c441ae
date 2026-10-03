from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL


# check_same_thread=False: FastAPI 可能在线程池中使用同一连接。
connect_args = (
    {"check_same_thread": False, "timeout": 30}
    if DATABASE_URL.startswith("sqlite")
    else {}
)
engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


# SQLite 默认 rollback journal 下，写事务串行执行：写锁冲突时等待
# 对端提交（busy timeout 30s）而不是立即 "database is locked"。
@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _record):
    if not DATABASE_URL.startswith("sqlite"):
        return
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA busy_timeout=30000")
    finally:
        cur.close()


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(bind=engine)
    _migrate_participants_required_tags()
    _migrate_tournament_status()
    _migrate_round_clock_tournament_paused()
    # 为升级前已存在的赛事补齐计数器（新库此时无赛事，为空操作）。
    # 之后序号一律经 EventCounter 的原子 UPSERT 分配。
    with SessionLocal() as db:
        from .models import Event, EventCounter

        missing = db.scalars(
            select(Event.tournament_id)
            .outerjoin(
                EventCounter,
                EventCounter.tournament_id == Event.tournament_id,
            )
            .where(EventCounter.tournament_id.is_(None))
            .distinct()
        ).all()
        if missing:
            from sqlalchemy import func

            seqs = dict(
                db.execute(
                    select(Event.tournament_id, func.max(Event.seq))
                    .where(Event.tournament_id.in_(missing))
                    .group_by(Event.tournament_id)
                ).all()
            )
            for tid in missing:
                db.add(EventCounter(tournament_id=tid, last_seq=seqs.get(tid, 0)))
            db.commit()


def _add_column_if_missing(table: str, column: str, ddl: str) -> None:
    """老库幂等补列（新库列已存在，为空操作）。"""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns(table)}
    if column in columns:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))


def _migrate_participants_required_tags() -> None:
    """为升级前已存在的 participants 表补 required_tags 列。

    create_all 只建新表、不改旧表；工位模块为参赛者新增了
    required_tags（必需标签）列，这里对老库做幂等的
    ALTER TABLE 补列（新库列已存在，为空操作）。
    """
    default = "'[]'::json" if engine.dialect.name == "postgresql" else "'[]'"
    _add_column_if_missing(
        "participants",
        "required_tags",
        f"required_tags JSON NOT NULL DEFAULT {default}",
    )


def _migrate_tournament_status() -> None:
    """为升级前已存在的 tournaments 表补 status 列。

    既有赛事统一视为 active（维护态由运营方显式切换进入）。
    """
    default = "'active'" if engine.dialect.name != "postgresql" else "'active'"
    _add_column_if_missing(
        "tournaments",
        "status",
        f"status VARCHAR(16) NOT NULL DEFAULT {default}",
    )


def _migrate_round_clock_tournament_paused() -> None:
    """为升级前已存在的 round_clocks 表补 tournament_paused 列。

    老库中 paused 的时钟均为手动暂停，默认 False：赛事 pause 不
    触碰它们，赛事 resume 也不会错误恢复。
    """
    default = "0" if engine.dialect.name != "postgresql" else "false"
    _add_column_if_missing(
        "round_clocks",
        "tournament_paused",
        f"tournament_paused BOOLEAN NOT NULL DEFAULT {default}",
    )
