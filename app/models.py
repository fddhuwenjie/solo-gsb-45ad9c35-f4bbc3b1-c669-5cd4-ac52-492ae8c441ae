from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Tournament(Base):
    __tablename__ = "tournaments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 生命周期状态：active（正常）/ paused（维护态，写操作一律 409）/
    # archived（归档，永久只读）。状态切换见 TournamentStateAction 与
    # app/services/lifecycle.py
    status: Mapped[str] = mapped_column(
        String(16), default="active", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    participants: Mapped[list["Participant"]] = relationship(
        back_populates="tournament",
        cascade="all, delete-orphan",
        order_by="Participant.number",
    )
    rounds: Mapped[list["Round"]] = relationship(
        back_populates="tournament",
        cascade="all, delete-orphan",
        order_by="Round.round_no",
    )
    stations: Mapped[list["Station"]] = relationship(
        back_populates="tournament",
        cascade="all, delete-orphan",
        order_by="Station.station_no",
    )
    events: Mapped[list["Event"]] = relationship(
        back_populates="tournament",
        cascade="all, delete-orphan",
        order_by="Event.seq",
    )
    checkin_snapshots: Mapped[list["CheckInSnapshot"]] = relationship(
        back_populates="tournament",
        cascade="all, delete-orphan",
        order_by="CheckInSnapshot.round_no",
    )


class CheckInSnapshot(Base):
    """目标轮次尚未生成时的现场签到快照。

    每个赛事、轮次只保留当前最新的一版；新的签到提交会版本递增并
    覆盖名单。轮次生成后快照冻结，不能再修改或确认。缺席参赛者保留
    既有积分和历史对局，但不参与目标轮次的配对/轮空，也不获得本轮
    积分。
    """

    __tablename__ = "checkin_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "tournament_id", "round_no", name="uq_checkin_snapshot_round"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False
    )
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # 当前版本：首次提交为 1，每次成功变更 +1
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 到场参赛者编号快照（按编号升序）
    present_numbers: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 缺席参赛者编号快照（按编号升序）
    absent_numbers: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 运营方是否已确认该快照；后续名单变更会重置为 False
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 轮次生成后为 True，用于冻结快照
    consumed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    tournament: Mapped[Tournament] = relationship(back_populates="checkin_snapshots")
    actions: Mapped[list["CheckInAction"]] = relationship(
        back_populates="snapshot",
        cascade="all, delete-orphan",
        order_by="CheckInAction.id",
    )


class CheckInAction(Base):
    """签到变更/确认请求的幂等记录：同一 request_id 重放原样返回。

    记录与快照变更、checkin.* 事件在同一事务中提交；被拒绝的请求
    （快照/序号冲突、名单重复或不完整、轮次已生成，409）不留记录。
    """

    __tablename__ = "checkin_actions"
    __table_args__ = (
        UniqueConstraint(
            "tournament_id", "request_id", name="uq_checkin_action_request"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("checkin_snapshots.id", ondelete="CASCADE"), nullable=True
    )
    # 客户端幂等键：赛事内唯一
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # submit（提交/变更签到名单）/ confirm（确认快照）
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    # 请求参数指纹：submit 含 round_no/expected_event_seq/两组名单；
    # confirm 含 round_no
    params: Mapped[dict] = mapped_column(JSON, nullable=False)
    # 首次执行后的响应快照：重复 request_id 原样返回
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    snapshot: Mapped[CheckInSnapshot | None] = relationship(back_populates="actions")


class Participant(Base):
    __tablename__ = "participants"
    __table_args__ = (
        UniqueConstraint("tournament_id", "number", name="uq_participant_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False
    )
    # 参赛者编号：赛事内从 1 开始，按录入顺序自增
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 必需标签：分配工位时，工位能力标签必须覆盖这些标签
    required_tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    tournament: Mapped[Tournament] = relationship(back_populates="participants")


class Station(Base):
    """赛事工位：编号、可用状态与能力标签。

    工位可随赛事维护（新增、停用、调整标签）；已发布的工位方案
    保存工位编号快照，不受后续变更影响。
    """

    __tablename__ = "stations"
    __table_args__ = (
        UniqueConstraint("tournament_id", "station_no", name="uq_station_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False
    )
    # 工位编号：赛事内唯一
    station_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # 是否可用：不可用工位不参与分配
    available: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    # 能力标签：该工位可满足的标签集合
    tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    tournament: Mapped[Tournament] = relationship(back_populates="stations")


class Round(Base):
    __tablename__ = "rounds"
    __table_args__ = (
        UniqueConstraint("tournament_id", "round_no", name="uq_round_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False
    )
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # open: 可继续录入赛果；closed: 全部赛果已录入
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    tournament: Mapped[Tournament] = relationship(back_populates="rounds")
    matches: Mapped[list["Match"]] = relationship(
        back_populates="round",
        cascade="all, delete-orphan",
        order_by="Match.id",
    )
    station_plans: Mapped[list["StationPlan"]] = relationship(
        back_populates="round",
        cascade="all, delete-orphan",
        order_by="StationPlan.version",
    )


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        CheckConstraint(
            "player_white_id IS NOT NULL OR player_black_id IS NOT NULL",
            name="ck_match_has_player",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    round_id: Mapped[int] = mapped_column(
        ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False
    )
    # 奇数人轮空：player_white_id 为轮空选手，player_black_id 为 NULL
    player_white_id: Mapped[int | None] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), nullable=True
    )
    player_black_id: Mapped[int | None] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), nullable=True
    )
    # 双方得分（各为 0 / 1 / 2 个半分，即 0 / 0.5 / 1）；
    # 轮空场次：轮空方 score_white=2、score_black=NULL
    score_white: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_black: Mapped[int | None] = mapped_column(Integer, nullable=True)

    round: Mapped[Round] = relationship(back_populates="matches")
    white: Mapped[Participant | None] = relationship(
        foreign_keys=[player_white_id]
    )
    black: Mapped[Participant | None] = relationship(
        foreign_keys=[player_black_id]
    )


class StationPlan(Base):
    """一轮的工位分配方案：版本化、发布后不可变。

    每轮可发布多个版本（version 从 1 递增）；未分配对局的说明
    以求解时的工位/标签状态快照保存，后续工位变更不影响已发布
    方案。
    """

    __tablename__ = "station_plans"
    __table_args__ = (
        UniqueConstraint("round_id", "version", name="uq_station_plan_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    round_id: Mapped[int] = mapped_column(
        ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False
    )
    # 方案版本：轮次内从 1 开始递增
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 未分配对局说明快照：
    # [{match_id, required_tags, missing_tags, conflicting_stations, reason}]
    unassigned: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    round: Mapped[Round] = relationship(back_populates="station_plans")
    assignments: Mapped[list["StationPlanAssignment"]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="StationPlanAssignment.match_id",
    )


class StationPlanAssignment(Base):
    """方案中的一条分配：一场非轮空对局占用一个工位。"""

    __tablename__ = "station_plan_assignments"
    __table_args__ = (
        UniqueConstraint("plan_id", "match_id", name="uq_plan_match"),
        UniqueConstraint("plan_id", "station_no", name="uq_plan_station"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("station_plans.id", ondelete="CASCADE"), nullable=False
    )
    match_id: Mapped[int] = mapped_column(
        ForeignKey("matches.id", ondelete="CASCADE"), nullable=False
    )
    station_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=False
    )
    # 工位编号快照：工位后续变更不影响已发布方案
    station_no: Mapped[int] = mapped_column(Integer, nullable=False)

    plan: Mapped[StationPlan] = relationship(back_populates="assignments")


class RoundClock(Base):
    """一轮的计时状态：每轮至多一行，由 start 命令创建。

    剩余时间不在数据库中逐秒更新，而是在读取时由
    started_at（服务端 UTC 锚点）、accumulated_pause_seconds
    （累计暂停时长）与 paused_at/ended_at 推算；没有任何后台
    写库任务，进程/容器重启后读数自然连续。
    """

    __tablename__ = "round_clocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    round_id: Mapped[int] = mapped_column(
        ForeignKey("rounds.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    # 乐观并发版本：每个成功应用的命令 +1；start 之前行不存在，视为 0
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # running / paused / ended（行由 start 创建，不存在 idle 行）
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # start 设定的轮次总时长（秒）
    total_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    # UTC 锚点：计时起点（naive UTC，与库内其他时间戳一致）
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # 累计暂停时长（秒）：resume/封盘时把本段暂停时长计入
    accumulated_pause_seconds: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False
    )
    # 暂停时刻（仅 paused 状态有值）；恢复时清空并计入累计暂停
    paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 当前暂停是否由赛事维护态（tournament pause）触发：True 时由赛事
    # resume 恢复；False（手动暂停）时赛事 resume 不动它
    paused_by_maintenance: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # 轮次封盘时刻（仅 ended 状态有值）：冻结剩余时间
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 各非轮空对局的累计加时：{"<match_id>": seconds}
    match_extensions: Mapped[dict] = mapped_column(
        JSON, default=dict, nullable=False
    )

    round: Mapped[Round] = relationship()


class ClockAction(Base):
    """计时命令的幂等记录：同一 request_id 重放时原样返回首次结果。

    记录与计时状态变更、round_clock.* 事件在同一事务中提交；
    被拒绝的命令（非法状态/版本冲突 409）不落任何记录。
    """

    __tablename__ = "clock_actions"
    __table_args__ = (
        UniqueConstraint("clock_id", "request_id", name="uq_clock_action_request"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clock_id: Mapped[int] = mapped_column(
        ForeignKey("round_clocks.id", ondelete="CASCADE"), nullable=False
    )
    # 客户端幂等键：同一时钟内唯一
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # start / pause / resume / extend
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 命令参数指纹：start {"total_seconds": n}；
    # extend {"match_id": m, "seconds": s}；pause/resume {}
    params: Mapped[dict] = mapped_column(JSON, nullable=False)
    # 首次执行后的响应快照：重复 request_id 原样返回
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    clock: Mapped[RoundClock] = relationship()


class TournamentStateAction(Base):
    """赛事状态切换（pause/resume/archive）请求的幂等记录。

    request_id 在赛事内唯一；记录与赛事状态变更、被冻结/恢复的轮次
    时钟、tournament.* 事件在同一事务中提交。被拒绝的切换（非法状态
    迁移、归档前置条件不满足、序号快照冲突，409）不落任何记录。
    """

    __tablename__ = "tournament_state_actions"
    __table_args__ = (
        UniqueConstraint(
            "tournament_id", "request_id", name="uq_tournament_state_action_request"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False
    )
    # 客户端幂等键：赛事内唯一
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # pause / resume / archive
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    # 请求参数指纹：action + expected_event_seq
    params: Mapped[dict] = mapped_column(JSON, nullable=False)
    # 首次执行后的响应快照：重复 request_id 原样返回
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


class EventCounter(Base):
    """赛事事件序号计数器：每个赛事一行。

    序号通过对该行的原子 UPSERT（INSERT ... ON CONFLICT DO UPDATE
    ... RETURNING）在当前写事务内分配，不再使用「查询最大 seq 后
    插入」的方式：并发事务在数据库层互斥排队，提交的序号因此唯一
    且连续；事务回滚时计数器的增量一并回滚，不留空号。
    """

    __tablename__ = "event_counters"

    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # 该赛事已分配的最大事件序号；行不存在表示尚未分配（下一条为 1）
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False)


class Event(Base):
    """赛事事件：序号在每个赛事内独立且单调递增。

    事件与引发它的业务变更在同一数据库事务中写入，序号由
    EventCounter 在事务内原子分配；事务回滚则事件与序号增量
    一并回滚。
    """

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("tournament_id", "seq", name="uq_event_tournament_seq"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False
    )
    # 赛事内单调递增序号，从 1 开始
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # 事件类型，见 app/events/ 中 TOURNAMENT_CREATED 等常量
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    # 事件载荷：事件类型、发生时间之外的相关资源标识
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    tournament: Mapped[Tournament] = relationship(back_populates="events")


class Announcement(Base):
    """定向赛务公告：版本化，公告本体不可改写。

    发布即生成公告行与第 1 版 AnnouncementRevision（同时冻结收件人
    快照）；需要更改时只能经 POST .../revisions 生成新版本并重新
    冻结收件人，旧版本与旧版回执原样保留、不沿用到新版。
    """

    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    tournament: Mapped[Tournament] = relationship()
    revisions: Mapped[list["AnnouncementRevision"]] = relationship(
        back_populates="announcement",
        cascade="all, delete-orphan",
        order_by="AnnouncementRevision.version",
    )
    actions: Mapped[list["AnnouncementAction"]] = relationship(
        back_populates="announcement",
        cascade="all, delete-orphan",
        order_by="AnnouncementAction.id",
    )


class AnnouncementRevision(Base):
    """公告的一个版本：正文、收件规则快照与冻结时点。

    rules 保存规范化后的收件规则快照；收件人在发布/修订的同一写
    事务内解析、去重并冻结到 announcement_recipients，之后排名、
    工位方案等任何变化都不影响该版本。
    """

    __tablename__ = "announcement_revisions"
    __table_args__ = (
        UniqueConstraint(
            "announcement_id", "version", name="uq_announcement_version"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    announcement_id: Mapped[int] = mapped_column(
        ForeignKey("announcements.id", ondelete="CASCADE"), nullable=False
    )
    # 公告内版本：从 1 开始递增
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 公告正文（该版本不可变）
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # 收件规则快照：
    # {participant_ids, rank_ranges: [{rank_from, rank_to}],
    #  station_plans: [{round_no, version, station_nos}],
    #  exclude_participant_ids}
    rules: Mapped[dict] = mapped_column(JSON, nullable=False)
    # 发布前断言的赛事事件序号（乐观快照，不一致不落库）
    expected_event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    announcement: Mapped[Announcement] = relationship(back_populates="revisions")
    recipients: Mapped[list["AnnouncementRecipient"]] = relationship(
        back_populates="revision",
        cascade="all, delete-orphan",
        order_by="AnnouncementRecipient.id",
    )
    acknowledgements: Mapped[list["AnnouncementAcknowledgement"]] = relationship(
        back_populates="revision",
        cascade="all, delete-orphan",
        order_by="AnnouncementAcknowledgement.id",
    )


class AnnouncementRecipient(Base):
    """某版公告冻结的一位收件人（去重后）。"""

    __tablename__ = "announcement_recipients"
    __table_args__ = (
        UniqueConstraint(
            "revision_id", "participant_id", name="uq_announcement_recipient"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("announcement_revisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )

    revision: Mapped[AnnouncementRevision] = relationship(
        back_populates="recipients"
    )


class AnnouncementAcknowledgement(Base):
    """收件人对某版公告的确认回执：每版每人至多一行。

    旧版本的回执挂在旧 revision 上，发布新版本后不会沿用。
    """

    __tablename__ = "announcement_acknowledgements"
    __table_args__ = (
        UniqueConstraint(
            "revision_id", "participant_id", name="uq_announcement_ack"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("announcement_revisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    revision: Mapped[AnnouncementRevision] = relationship(
        back_populates="acknowledgements"
    )


class AnnouncementAction(Base):
    """公告发布/修订请求的幂等记录：同一 request_id 重放原样返回。

    request_id 在赛事内唯一；记录与公告版本行、冻结收件人、
    announcement.* 事件在同一事务中提交。规则无效/快照冲突等被
    拒绝的请求（409）不落任何记录。
    """

    __tablename__ = "announcement_actions"
    __table_args__ = (
        UniqueConstraint(
            "tournament_id", "request_id", name="uq_announcement_action_request"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False
    )
    announcement_id: Mapped[int] = mapped_column(
        ForeignKey("announcements.id", ondelete="CASCADE"), nullable=False
    )
    revision_id: Mapped[int] = mapped_column(
        ForeignKey("announcement_revisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 客户端幂等键：赛事内唯一
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # publish / revise
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    # 请求参数指纹：action/body/expected_event_seq/rules，
    # revise 另含 announcement_id
    params: Mapped[dict] = mapped_column(JSON, nullable=False)
    # 首次执行后的响应快照：重复 request_id 原样返回
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    announcement: Mapped[Announcement] = relationship(back_populates="actions")


class ResultReview(Base):
    """已提交赛果的复核申请：一场对局同时至多有一条待处理复核。

    创建时冻结原比分（original_score_*）并记录拟更正比分
    （proposed_score_*）与理由；待处理（pending）期间原比分不可
    修改、赛事不能生成下一轮。双方选手分别同意/驳回
    （result_review_decisions 每方至多一行，改票原地更新）：
    任一驳回即以 rejected 关闭、比分不变；双方同意时在同一写事务内
    更正比分并以 approved 关闭。关闭后保留原比分、提议与全部决定。
    每场对局可有若干已关闭复核，待处理复核由部分唯一索引保证唯一。
    """

    __tablename__ = "result_reviews"
    __table_args__ = (
        Index(
            "uq_result_review_pending_match",
            "match_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(
        ForeignKey("matches.id", ondelete="CASCADE"), nullable=False
    )
    # 申请前断言的赛事当前最大事件序号（乐观快照，不一致不落库）
    expected_event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # 创建时冻结的原比分（半分单位 0 / 1 / 2）
    original_score_white: Mapped[int] = mapped_column(Integer, nullable=False)
    original_score_black: Mapped[int] = mapped_column(Integer, nullable=False)
    # 拟更正比分（半分单位 0 / 1 / 2，双方之和为 2）
    proposed_score_white: Mapped[int] = mapped_column(Integer, nullable=False)
    proposed_score_black: Mapped[int] = mapped_column(Integer, nullable=False)
    # 更正理由
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # pending（待双方决定）/ approved（双方同意，比分已更正）/
    # rejected（任一驳回，比分不变）
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    # 关闭时刻（approved/rejected）；pending 期间为 NULL
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    match: Mapped[Match] = relationship()
    decisions: Mapped[list["ResultReviewDecision"]] = relationship(
        back_populates="review",
        cascade="all, delete-orphan",
        order_by="ResultReviewDecision.id",
    )


class ResultReviewDecision(Base):
    """对局一方选手对复核的决定：每条复核每位选手至多一行。

    重复提交相同决定原样返回首次结果；改票（pending 期间）原地
    更新 vote，不产生额外记录。created_at 为首次决定时间，
    updated_at 为最近一次改票时间（未改票时与 created_at 相同）。
    """

    __tablename__ = "result_review_decisions"
    __table_args__ = (
        UniqueConstraint(
            "review_id", "participant_id", name="uq_result_review_participant"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    review_id: Mapped[int] = mapped_column(
        ForeignKey("result_reviews.id", ondelete="CASCADE"), nullable=False
    )
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    # approve（同意）/ reject（驳回）
    vote: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    review: Mapped[ResultReview] = relationship(back_populates="decisions")
    participant: Mapped[Participant] = relationship()


class PenaltyEntry(Base):
    """扣分账本条目：登记扣分（deduct）或撤销一笔扣分（revoke）。

    账本只增不改：撤销不删除原扣分，而是追加一条 revoke 条目并把
    被撤销扣分的 status 置为 revoked（同一事务内完成）；有效扣分
    即 status=active 的 deduct 条目之和。条目同时充当幂等记录：
    request_id 在赛事内唯一，重复请求按 params 指纹判定——一致则
    原样返回 response 快照，不一致 409。被拒绝的请求（资格不符/
    快照冲突/重复撤销，409）不落任何记录。
    """

    __tablename__ = "penalty_entries"
    __table_args__ = (
        UniqueConstraint(
            "tournament_id", "request_id", name="uq_penalty_entry_request"
        ),
        # 一笔扣分至多被撤销一次（数据库层兜底，重复撤销不落库）
        Index(
            "uq_penalty_revoke_target",
            "revokes_entry_id",
            unique=True,
            postgresql_where=text("revokes_entry_id IS NOT NULL"),
            sqlite_where=text("revokes_entry_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False
    )
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    # 客户端幂等键：赛事内唯一
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # 登记前断言的赛事当前最大事件序号（乐观快照，不一致不落库）
    expected_event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # deduct（登记扣分）/ revoke（撤销一笔扣分）
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # 扣分量（半分单位的正整数）；revoke 条目冗余记录被撤销的量，
    # 使账本单行即可读（被撤销的扣分条目不可变，冗余不会漂移）
    points: Mapped[int] = mapped_column(Integer, nullable=False)
    # 扣分/撤销理由
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # 撤销目标（仅 revoke 条目）：被撤销的 deduct 条目 id
    revokes_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("penalty_entries.id", ondelete="CASCADE"), nullable=True
    )
    # deduct：active（生效中）/ revoked（已被撤销）；
    # revoke：applied（撤销条目本身不可再被撤销）
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # 首次执行后的响应快照：重复 request_id 原样返回
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    participant: Mapped[Participant] = relationship()


class PromotionList(Base):
    """晋级名单版本：发布即冻结，后续排名与扣分变化不改写该版本。

    发布时在同一事务内重算资格轮次（含）之前的排名积分与 Buchholz，
    按排名积分 → Buchholz → 参赛者编号固定裁决，冻结前 slots 名为
    晋级席位、其余参赛者按同一次序为候补。席位/候补行保存冻结依据，
    放弃与递补只更新席位的当前持有者与候补状态。
    """

    __tablename__ = "promotion_lists"
    __table_args__ = (
        UniqueConstraint(
            "tournament_id", "version", name="uq_promotion_list_version"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False
    )
    # 赛事内版本：从 1 开始递增
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 资格轮次：排名积分与 Buchholz 只统计第 1..qualifying_round_no 轮
    qualifying_round_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # 晋级名额（席位总数）
    slots: Mapped[int] = mapped_column(Integer, nullable=False)
    # 发布前断言的赛事当前最大事件序号（乐观快照，不一致不落库）
    expected_event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    seats: Mapped[list["PromotionSeat"]] = relationship(
        back_populates="promotion_list",
        cascade="all, delete-orphan",
        order_by="PromotionSeat.seat_no",
    )
    waitlist_entries: Mapped[list["PromotionWaitlistEntry"]] = relationship(
        back_populates="promotion_list",
        cascade="all, delete-orphan",
        order_by="PromotionWaitlistEntry.position",
    )


class PromotionSeat(Base):
    """晋级席位：原始持有者与排名依据发布时冻结，当前持有者随放弃/递补变化。"""

    __tablename__ = "promotion_seats"
    __table_args__ = (
        UniqueConstraint("list_id", "seat_no", name="uq_promotion_seat_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    list_id: Mapped[int] = mapped_column(
        ForeignKey("promotion_lists.id", ondelete="CASCADE"), nullable=False
    )
    # 席位号：1..slots，按发布时冻结排名顺序
    seat_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # 发布时冻结的原始持有者与排名依据（半分单位，发布后不改写）
    original_participant_id: Mapped[int] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    original_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    original_rank_points: Mapped[int] = mapped_column(Integer, nullable=False)
    original_buchholz: Mapped[int] = mapped_column(Integer, nullable=False)
    # 当前持有者：放弃后由最前候补递补；无候补可递补时为 NULL（空席）
    current_participant_id: Mapped[int | None] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), nullable=True
    )
    # active（有当前有效晋级者）/ vacant（持有者放弃且无候补，空席）
    status: Mapped[str] = mapped_column(
        String(16), default="active", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    promotion_list: Mapped[PromotionList] = relationship(back_populates="seats")


class PromotionWaitlistEntry(Base):
    """候补顺序条目：发布时冻结排名依据，递补时按 position 升序取最前者。"""

    __tablename__ = "promotion_waitlist_entries"
    __table_args__ = (
        UniqueConstraint(
            "list_id", "position", name="uq_promotion_waitlist_position"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    list_id: Mapped[int] = mapped_column(
        ForeignKey("promotion_lists.id", ondelete="CASCADE"), nullable=False
    )
    # 候补顺位：从 1 开始，按发布时冻结排名顺序
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), nullable=False
    )
    # 发布时冻结的排名依据（半分单位，发布后不改写）
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    rank_points: Mapped[int] = mapped_column(Integer, nullable=False)
    buchholz: Mapped[int] = mapped_column(Integer, nullable=False)
    # waiting（候补中）/ promoted（已递补为某席位当前持有者）
    status: Mapped[str] = mapped_column(
        String(16), default="waiting", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    promotion_list: Mapped[PromotionList] = relationship(
        back_populates="waitlist_entries"
    )


class PromotionAction(Base):
    """晋级名单发布/放弃请求的幂等记录：同一 request_id 重放原样返回。

    request_id 在赛事内唯一（publish 与 forfeit 共用同一命名空间）；
    记录与名单版本、席位/候补变更、promotion.* 事件在同一事务中提交。
    被拒绝的请求（轮次未结束、有待处理复核、名额非法、快照冲突、
    非当前有效晋级者等，409）不落任何记录。
    """

    __tablename__ = "promotion_actions"
    __table_args__ = (
        UniqueConstraint(
            "tournament_id", "request_id", name="uq_promotion_action_request"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False
    )
    list_id: Mapped[int] = mapped_column(
        ForeignKey("promotion_lists.id", ondelete="CASCADE"), nullable=False
    )
    # 客户端幂等键：赛事内唯一
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # publish（发布名单）/ forfeit（标记放弃并递补）
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    # 请求参数指纹：publish 含 qualifying_round_no/slots/
    # expected_event_seq；forfeit 含 list_id/participant_id/reason
    params: Mapped[dict] = mapped_column(JSON, nullable=False)
    # 首次执行后的响应快照：重复 request_id 原样返回
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    promotion_list: Mapped[PromotionList] = relationship()


class ResultReviewAction(Base):
    """复核创建请求的幂等记录：同一 request_id 重放原样返回。

    request_id 在对局内唯一；记录与复核行、result_review.created
    事件在同一事务中提交。资格不符/快照冲突等被拒绝的请求（409）
    不落任何记录。
    """

    __tablename__ = "result_review_actions"
    __table_args__ = (
        UniqueConstraint(
            "match_id", "request_id", name="uq_result_review_action_request"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(
        ForeignKey("matches.id", ondelete="CASCADE"), nullable=False
    )
    review_id: Mapped[int] = mapped_column(
        ForeignKey("result_reviews.id", ondelete="CASCADE"), nullable=False
    )
    # 客户端幂等键：对局内唯一
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # 请求参数指纹：expected_event_seq / 拟更正比分 / 理由
    params: Mapped[dict] = mapped_column(JSON, nullable=False)
    # 首次执行后的响应快照：重复 request_id 原样返回
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
