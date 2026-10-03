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
    _migrate_tournament_lifecycle()
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
    """对已存在的表幂等地补列（create_all 只建新表、不改旧表）。"""
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

    工位模块为参赛者新增了 required_tags（必需标签）列，这里对老库做
    幂等的 ALTER TABLE 补列（新库列已存在，为空操作）。
    """
    default = "'[]'::json" if engine.dialect.name == "postgresql" else "'[]'"
    _add_column_if_missing(
        "participants",
        "required_tags",
        f"required_tags JSON NOT NULL DEFAULT {default}",
    )


def _migrate_tournament_lifecycle() -> None:
    """为升级前已存在的库补赛事生命周期相关列（幂等）。

    - tournaments.status：赛事状态（active/paused/archived），老赛事
      一律视为 active；
    - round_clocks.paused_by_maintenance：区分手动暂停与赛事维护态
      冻结的时钟，老库无维护态暂停，一律为 False。
    """
    bool_default = "FALSE" if engine.dialect.name == "postgresql" else "0"
    _add_column_if_missing(
        "tournaments",
        "status",
        "status VARCHAR(16) NOT NULL DEFAULT 'active'",
    )
    _add_column_if_missing(
        "round_clocks",
        "paused_by_maintenance",
        f"paused_by_maintenance BOOLEAN NOT NULL DEFAULT {bool_default}",
    )
