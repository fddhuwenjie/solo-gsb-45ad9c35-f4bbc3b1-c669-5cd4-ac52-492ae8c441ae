from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)


def _normalize_tags(tags: list[str]) -> list[str]:
    """标签规范化：去空白、去重、排序；空标签视为非法。"""
    cleaned = sorted({t.strip() for t in tags})
    if any(not t for t in cleaned):
        raise ValueError("标签去空白后不能为空")
    if len(cleaned) > 32:
        raise ValueError("标签数量不能超过 32 个")
    if any(len(t) > 64 for t in cleaned):
        raise ValueError("单个标签长度不能超过 64 个字符")
    return cleaned


# 能力/必需标签列表：自动去空白、去重、排序
Tags = Annotated[list[str], AfterValidator(_normalize_tags)]


# ---------- Tournament ----------

class TournamentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="赛事名称")


class TournamentResponse(BaseModel):
    id: int
    name: str
    # 赛事状态：active（正常）/ paused（维护态）/ archived（归档，永久只读）
    status: str

    model_config = {"from_attributes": True}


# ---------- Tournament lifecycle（状态控制） ----------

class TournamentStatusActionRequest(BaseModel):
    """赛事状态切换命令：一个请求执行一次切换。

    - pause：active -> paused，进入维护态并冻结所有运行中的轮次时钟；
    - resume：paused -> active，仅恢复被赛事暂停的时钟；
    - archive：paused -> archived，要求所有轮次已结束且无待处理复核，
      归档后永久只读。
    """

    request_id: str = Field(
        min_length=1, max_length=128, description="幂等键：重复提交返回首次结果"
    )
    expected_event_seq: int = Field(
        ge=0,
        description="期望的赛事当前最大事件序号；不一致返回 409 且不落库",
    )
    action: Literal["pause", "resume", "archive"]


class StatusClockBrief(BaseModel):
    """一次状态切换中受影响（被冻结/被恢复）的轮次时钟。"""

    round_id: int
    round_no: int


class TournamentStatusResponse(BaseModel):
    tournament_id: int
    # 切换后的赛事状态：active / paused / archived
    status: str
    # 切换前的赛事状态
    previous_status: str
    # 本次执行的命令：pause / resume / archive
    action: str
    request_id: str
    expected_event_seq: int
    # 本次切换写入的 tournament.* 事件序号
    event_seq: int
    # pause 时被赛事冻结的运行中时钟（resume/archive 时为空）
    frozen_clocks: list[StatusClockBrief] = []
    # resume 时被恢复的赛事暂停时钟（pause/archive 时为空）；
    # 手动暂停的时钟不在其列
    resumed_clocks: list[StatusClockBrief] = []
    # 切换发生的服务端 UTC 时间（ISO 8601，Z 结尾）
    changed_at: datetime

    @field_serializer("changed_at")
    def serialize_changed_at(self, v: datetime) -> str:
        return _serialize_utc(v)


# ---------- Participant ----------

class ParticipantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="参赛者姓名")
    required_tags: Tags = Field(
        default_factory=list, description="参赛者必需标签（工位须覆盖）"
    )


class ParticipantTagsUpdate(BaseModel):
    required_tags: Tags = Field(description="参赛者必需标签（整体替换）")


class ParticipantsBulkCreate(BaseModel):
    names: list[str] = Field(
        min_length=1,
        description="参赛者姓名列表，编号按此顺序从 1 开始分配",
    )


class ParticipantResponse(BaseModel):
    id: int
    number: int
    name: str
    required_tags: list[str] = []

    model_config = {"from_attributes": True}


# ---------- Match / Round ----------

class MatchResponse(BaseModel):
    id: int
    player_white: ParticipantResponse | None = None
    player_black: ParticipantResponse | None = None
    # 0 / 0.5 / 1；轮空方 1，另一方为 None
    score_white: float | None = None
    score_black: float | None = None
    is_bye: bool = False


class RoundResponse(BaseModel):
    id: int
    round_no: int
    status: str
    matches: list[MatchResponse] = []


# ---------- Check-in ----------

class CheckInSnapshotRequest(BaseModel):
    request_id: str = Field(
        min_length=1, max_length=128, description="幂等键：重复提交返回首次结果"
    )
    expected_event_seq: int = Field(
        ge=0,
        description="期望的赛事当前最大事件序号；不一致返回 409 且不落库",
    )
    round_no: int = Field(ge=1, description="尚未生成的目标轮次号")
    present_numbers: list[int] = Field(
        description="到场参赛者编号；与缺席名单合并后必须恰好覆盖当前报名名单"
    )
    absent_numbers: list[int] = Field(
        description="缺席参赛者编号；与到场名单互不相交"
    )

    @field_validator("present_numbers", "absent_numbers")
    @classmethod
    def _check_positive_numbers(cls, v: list[int]) -> list[int]:
        if any(n < 1 for n in v):
            raise ValueError("参赛者编号必须为正整数")
        return v


class CheckInConfirmRequest(BaseModel):
    request_id: str = Field(
        min_length=1, max_length=128, description="幂等键：重复提交返回首次结果"
    )
    expected_event_seq: int = Field(
        ge=0,
        description="期望的赛事当前最大事件序号；不一致返回 409 且不落库",
    )
    round_no: int = Field(ge=1, description="要确认的目标轮次号")


class CheckInParticipantResponse(BaseModel):
    participant_id: int
    number: int
    name: str
    attended: bool


class CheckInSnapshotResponse(BaseModel):
    id: int
    tournament_id: int
    round_no: int
    # 首次提交为 1，每次成功变更签到名单 +1
    version: int
    present_numbers: list[int]
    absent_numbers: list[int]
    roster: list[CheckInParticipantResponse]
    # 是否已由运营方确认；名单变更后重置为 False
    confirmed: bool
    # 目标轮次生成后冻结为 True
    consumed: bool
    confirmed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @field_serializer("confirmed_at", "created_at", "updated_at")
    def serialize_times(self, v: datetime | None) -> str | None:
        return _serialize_utc(v)


class ResultUpdate(BaseModel):
    # 0 / 1 / 2 个半分，即允许 0、0.5、1
    score_white: float = Field(ge=0, le=1, description="小白方得分：0 / 0.5 / 1")
    score_black: float = Field(ge=0, le=1, description="小黑方得分：0 / 0.5 / 1")


# ---------- Standing ----------

class StandingResponse(BaseModel):
    rank: int
    participant_id: int
    number: int
    name: str
    # 对局积分（赛果累计，不含扣分）
    points: float
    # 当前有效扣分（账本中生效扣分条目之和）
    penalty: float
    # 排名积分 = 对局积分 - 有效扣分（允许为负）；
    # 排序、Buchholz 与下一轮积分分组均以此为准
    rank_points: float
    # Buchholz：实际对手的排名积分之和（随对手扣分重算）
    buchholz: float
    played: int
    byes: int


# ---------- Event ----------

class EventResponse(BaseModel):
    tournament_id: int
    # 赛事内单调递增序号（每赛事独立，从 1 开始）
    seq: int
    type: str
    # 相关资源标识（如 tournament_id / participant_id / round_id 等）
    payload: dict
    # UTC 发生时间，ISO 8601（Z 结尾）
    occurred_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("occurred_at")
    def serialize_occurred_at(self, v: datetime) -> str:
        # 存储为 naive UTC，序列化为带 Z 后缀的 ISO 8601
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class EventPageResponse(BaseModel):
    items: list[EventResponse]
    # 本页最后一条事件序号；本页为空时等于请求的 after
    last_seq: int
    # 之后是否还有更多事件
    has_more: bool


# ---------- Station ----------

class StationCreate(BaseModel):
    station_no: int = Field(ge=1, description="工位编号（赛事内唯一）")
    available: bool = Field(default=True, description="是否可用")
    tags: Tags = Field(default_factory=list, description="工位能力标签")


class StationsBulkCreate(BaseModel):
    stations: list[StationCreate] = Field(
        min_length=1, description="工位列表，全部成功才落库"
    )


class StationUpdate(BaseModel):
    available: bool = Field(description="是否可用")
    tags: Tags = Field(description="工位能力标签（整体替换）")


class StationResponse(BaseModel):
    id: int
    station_no: int
    available: bool
    tags: list[str]

    model_config = {"from_attributes": True}


# ---------- StationPlan ----------

class StationPin(BaseModel):
    """固定工位：指定某场非轮空对局必须使用的工位。"""

    match_id: int = Field(description="本轮非轮空对局 id")
    station_no: int = Field(description="工位编号")


class StationPlanCreate(BaseModel):
    expected_version: int = Field(
        ge=0,
        description="期望的当前最新版本号（尚无方案时为 0）；不一致返回 409",
    )
    pins: list[StationPin] = Field(
        default_factory=list, description="固定工位（必须全部满足）"
    )


class StationAssignmentResponse(BaseModel):
    match_id: int
    station_no: int


class UnassignedMatchResponse(BaseModel):
    match_id: int
    # 双方选手必需标签的并集
    required_tags: list[str]
    # 所有可用工位都不提供的标签
    missing_tags: list[str]
    # 能覆盖全部必需标签、但本方案中已被占用的工位编号
    conflicting_stations: list[int]
    # 人类可读的原因说明
    reason: str


class StationPlanResponse(BaseModel):
    id: int
    round_id: int
    round_no: int
    version: int
    assignments: list[StationAssignmentResponse]
    unassigned: list[UnassignedMatchResponse]
    # UTC 发布时间，ISO 8601（Z 结尾）
    created_at: datetime

    @field_serializer("created_at")
    def serialize_created_at(self, v: datetime) -> str:
        # 存储为 naive UTC，序列化为带 Z 后缀的 ISO 8601
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------- Round Clock ----------

def _serialize_utc(v: datetime | None) -> str | None:
    # 存储为 naive UTC，序列化为带 Z 后缀的 ISO 8601
    if v is None:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ClockActionRequest(BaseModel):
    """轮次计时命令：一个请求执行一个命令。

    - start：设定轮次总时长并启动计时（每轮仅一次）；
    - pause / resume：全局暂停 / 恢复（暂停期间所有对局冻结）；
    - extend：给指定非轮空对局加时（match_id + seconds）。
    """

    request_id: str = Field(
        min_length=1, max_length=128, description="幂等键：重复提交返回首次结果"
    )
    expected_version: int = Field(
        ge=0, description="期望的当前时钟版本（未启动时为 0）；不一致返回 409"
    )
    action: Literal["start", "pause", "resume", "extend"]
    # start 参数：轮次总时长（秒）
    total_seconds: int | None = Field(default=None, ge=1)
    # extend 参数：目标对局与加时秒数
    match_id: int | None = None
    seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check_action_params(self):
        if self.action == "start" and self.total_seconds is None:
            raise ValueError("start 命令必须提供 total_seconds")
        if self.action == "extend" and (
            self.match_id is None or self.seconds is None
        ):
            raise ValueError("extend 命令必须提供 match_id 和 seconds")
        return self


class ClockMatchResponse(BaseModel):
    match_id: int
    # 该场累计加时（extend 叠加，秒）
    extra_seconds: int
    # 该场截止时刻（UTC，Z 结尾）；暂停中未知（恢复后才确定）为 null，
    # 计时未启动也为 null
    deadline_at: datetime | None = None
    # 该场剩余秒数（暂停/封盘时为冻结值，超时为负）；未启动为 null
    remaining_seconds: int | None = None

    @field_serializer("deadline_at")
    def serialize_deadline_at(self, v: datetime | None) -> str | None:
        return _serialize_utc(v)


class ClockResponse(BaseModel):
    tournament_id: int
    round_id: int
    round_no: int
    # 时钟版本：每个成功命令 +1；未启动为 0
    version: int
    # idle（未启动）/ running / paused / ended（轮次已封盘）
    status: str
    total_seconds: int | None = None
    started_at: datetime | None = None
    paused_at: datetime | None = None
    ended_at: datetime | None = None
    accumulated_pause_seconds: float = 0.0
    # 服务端当前 UTC 时间，供客户端校准倒计时
    server_time: datetime
    # 本轮各非轮空对局的计时（轮空对局不计时）
    matches: list[ClockMatchResponse] = []

    @field_serializer("started_at", "paused_at", "ended_at", "server_time")
    def serialize_utc_fields(self, v: datetime | None) -> str | None:
        return _serialize_utc(v)


# ---------- Announcement ----------

def _dedup_sort_positive(values: list[int]) -> list[int]:
    cleaned = sorted(set(values))
    if any(v < 1 for v in cleaned):
        raise ValueError("id 必须为正整数")
    return cleaned


class AnnouncementRankRange(BaseModel):
    """当前排名闭区间（排名从 1 开始，含端点）。"""

    rank_from: int = Field(ge=1, description="区间起始排名（含）")
    rank_to: int = Field(ge=1, description="区间结束排名（含）")

    @model_validator(mode="after")
    def _check_order(self):
        if self.rank_from > self.rank_to:
            raise ValueError("排名区间起始不能大于结束")
        return self


class AnnouncementStationPlanRule(BaseModel):
    """按某版工位方案中指定工位号上的选手定向。"""

    round_no: int = Field(ge=1, description="轮次号")
    version: int = Field(ge=1, description="该轮工位方案版本号")
    station_nos: list[int] = Field(
        min_length=1, description="工位号列表（这些工位上的双方选手入列）"
    )

    @field_validator("station_nos")
    @classmethod
    def _normalize_station_nos(cls, v: list[int]) -> list[int]:
        return _dedup_sort_positive(v)


class AnnouncementRules(BaseModel):
    """收件规则：三类选择器取并集，再应用排除名单。

    participant_ids / rank_ranges / station_plans 至少提供一项；
    并集去重后剔除 exclude_participant_ids。规则引用的资源必须
    全部属于当前赛事，否则请求 409 且不落库。
    """

    participant_ids: list[int] = Field(
        default_factory=list, description="按参赛者 id 直接指定（并集）"
    )
    rank_ranges: list[AnnouncementRankRange] = Field(
        default_factory=list, description="按当前排名闭区间指定（并集）"
    )
    station_plans: list[AnnouncementStationPlanRule] = Field(
        default_factory=list,
        description="按某版工位方案的工位号指定（并集）",
    )
    exclude_participant_ids: list[int] = Field(
        default_factory=list, description="排除名单（并集之后剔除）"
    )

    @field_validator("participant_ids", "exclude_participant_ids")
    @classmethod
    def _normalize_ids(cls, v: list[int]) -> list[int]:
        return _dedup_sort_positive(v)

    @model_validator(mode="after")
    def _check_non_empty_union(self):
        if not (
            self.participant_ids
            or self.rank_ranges
            or self.station_plans
        ):
            raise ValueError(
                "收件规则至少需要 participant_ids / rank_ranges / "
                "station_plans 中的一项"
            )
        return self


class AnnouncementCreate(BaseModel):
    request_id: str = Field(
        min_length=1, max_length=128, description="幂等键：重复提交返回首次结果"
    )
    expected_event_seq: int = Field(
        ge=0,
        description="期望的赛事当前最大事件序号；不一致返回 409 且不落库",
    )
    body: str = Field(min_length=1, max_length=10_000, description="公告正文")
    rules: AnnouncementRules


class AnnouncementRevisionCreate(BaseModel):
    request_id: str = Field(
        min_length=1, max_length=128, description="幂等键：重复提交返回首次结果"
    )
    expected_event_seq: int = Field(
        ge=0,
        description="期望的赛事当前最大事件序号；不一致返回 409 且不落库",
    )
    body: str = Field(min_length=1, max_length=10_000, description="新版本正文")
    rules: AnnouncementRules


class AnnouncementAckRequest(BaseModel):
    participant_id: int = Field(ge=1, description="确认回执的参赛者 id")


class AnnouncementRulesResponse(BaseModel):
    participant_ids: list[int]
    rank_ranges: list[AnnouncementRankRange]
    station_plans: list[AnnouncementStationPlanRule]
    exclude_participant_ids: list[int]


class AnnouncementRevisionResponse(BaseModel):
    id: int
    announcement_id: int
    tournament_id: int
    version: int
    body: str
    rules: AnnouncementRulesResponse
    expected_event_seq: int
    # 发布时冻结的收件人数
    recipient_count: int
    # 已确认回执人数
    acknowledged_count: int
    created_at: datetime

    @field_serializer("created_at")
    def serialize_created_at(self, v: datetime) -> str:
        return _serialize_utc(v)


class AnnouncementResponse(BaseModel):
    id: int
    tournament_id: int
    # 当前最新版本号（从 1 递增）
    latest_version: int
    created_at: datetime
    latest_revision: AnnouncementRevisionResponse

    @field_serializer("created_at")
    def serialize_created_at(self, v: datetime) -> str:
        return _serialize_utc(v)


class AnnouncementRecipientResponse(BaseModel):
    participant_id: int
    number: int
    name: str

    model_config = {"from_attributes": True}


class AnnouncementAckResponse(BaseModel):
    announcement_id: int
    version: int
    participant_id: int
    number: int
    name: str
    # 首次确认时间；重复提交返回相同回执
    acknowledged_at: datetime

    @field_serializer("acknowledged_at")
    def serialize_acknowledged_at(self, v: datetime) -> str:
        return _serialize_utc(v)


class AnnouncementAckListResponse(BaseModel):
    announcement_id: int
    version: int
    # 已确认名单（按参赛者编号升序）
    acknowledged: list[AnnouncementRecipientResponse]
    # 未确认名单（按参赛者编号升序）
    pending: list[AnnouncementRecipientResponse]


# ---------- Result review ----------

class ResultReviewCreate(BaseModel):
    request_id: str = Field(
        min_length=1, max_length=128, description="幂等键：重复提交返回首次结果"
    )
    expected_event_seq: int = Field(
        ge=0,
        description="期望的赛事当前最大事件序号；不一致返回 409 且不落库",
    )
    # 拟更正比分：各为 0 / 0.5 / 1，双方之和必须为 1
    score_white: float = Field(ge=0, le=1, description="拟更正小白方得分")
    score_black: float = Field(ge=0, le=1, description="拟更正小黑方得分")
    reason: str = Field(min_length=1, max_length=10_000, description="更正理由")


class ResultReviewDecisionRequest(BaseModel):
    participant_id: int = Field(ge=1, description="作出决定的选手（须为本场选手）")
    vote: Literal["approve", "reject"] = Field(
        description="approve 同意更正 / reject 驳回"
    )


class ResultReviewDecisionResponse(BaseModel):
    id: int
    participant_id: int
    number: int
    name: str
    # approve / reject（改票后反映最新选择）
    vote: str
    # 首次决定时间；改票不改变该时间
    decided_at: datetime
    # 最近一次改票时间（未改票时与 decided_at 相同）
    updated_at: datetime

    @field_serializer("decided_at", "updated_at")
    def serialize_decided_at(self, v: datetime) -> str:
        return _serialize_utc(v)


class ResultReviewResponse(BaseModel):
    id: int
    match_id: int
    tournament_id: int
    round_id: int
    round_no: int
    player_white: ParticipantResponse
    player_black: ParticipantResponse
    expected_event_seq: int
    # 创建时冻结的原比分
    original_score_white: float
    original_score_black: float
    # 拟更正比分
    proposed_score_white: float
    proposed_score_black: float
    reason: str
    # pending / approved / rejected
    status: str
    decisions: list[ResultReviewDecisionResponse] = []
    created_at: datetime
    closed_at: datetime | None = None

    @field_serializer("created_at", "closed_at")
    def serialize_times(self, v: datetime | None) -> str | None:
        return _serialize_utc(v)


# ---------- Penalty ledger ----------

def _validate_half_point_multiple(v: float) -> float:
    """扣分量必须是 0.5 的整数倍（半分单位的正整数）。"""
    doubled = v * 2
    if abs(doubled - round(doubled)) > 1e-9:
        raise ValueError("扣分必须是 0.5 的整数倍")
    return v


class PenaltyDeductRequest(BaseModel):
    """登记一笔扣分。"""

    request_id: str = Field(
        min_length=1, max_length=128, description="幂等键：重复提交返回首次结果"
    )
    expected_event_seq: int = Field(
        ge=0,
        description="期望的赛事当前最大事件序号；不一致返回 409 且不落库",
    )
    participant_id: int = Field(ge=1, description="被扣分的参赛者 id")
    points: float = Field(gt=0, description="扣分量：0.5 的整数倍")
    reason: str = Field(min_length=1, max_length=10_000, description="扣分理由")

    @field_validator("points")
    @classmethod
    def _check_points(cls, v: float) -> float:
        return _validate_half_point_multiple(v)


class PenaltyRevokeRequest(BaseModel):
    """以新记录撤销一笔仍生效的扣分。"""

    request_id: str = Field(
        min_length=1, max_length=128, description="幂等键：重复提交返回首次结果"
    )
    expected_event_seq: int = Field(
        ge=0,
        description="期望的赛事当前最大事件序号；不一致返回 409 且不落库",
    )
    reason: str = Field(min_length=1, max_length=10_000, description="撤销理由")


class PenaltyEntryResponse(BaseModel):
    id: int
    tournament_id: int
    participant_id: int
    number: int
    name: str
    # deduct（登记扣分）/ revoke（撤销一笔扣分）
    kind: str
    # 扣分量；revoke 条目为被撤销的量
    points: float
    reason: str
    # deduct：active / revoked；revoke：applied
    status: str
    # 仅 revoke 条目：被撤销的扣分条目 id
    revokes_entry_id: int | None = None
    expected_event_seq: int
    created_at: datetime

    @field_serializer("created_at")
    def serialize_created_at(self, v: datetime) -> str:
        return _serialize_utc(v)


class ParticipantPenaltyLedgerResponse(BaseModel):
    """参赛者的完整扣分账本：逐条记录 + 当前有效扣分合计。"""

    participant_id: int
    number: int
    name: str
    # 当前有效扣分（生效中的 deduct 条目之和）
    effective_penalty: float
    # 全部账本条目（含已撤销的扣分与撤销记录，按登记顺序）
    entries: list[PenaltyEntryResponse]


# ---------- Promotion list ----------

class PromotionPublishRequest(BaseModel):
    """发布一份晋级名单（赛事内版本递增）。"""

    request_id: str = Field(
        min_length=1, max_length=128, description="幂等键：重复提交返回首次结果"
    )
    expected_event_seq: int = Field(
        ge=0,
        description="期望的赛事当前最大事件序号；不一致返回 409 且不落库",
    )
    qualifying_round_no: int = Field(
        ge=1, description="资格轮次：排名积分与 Buchholz 统计到该轮（含）为止"
    )
    slots: int = Field(
        ge=1, description="晋级名额：按冻结排名取前 N 名为晋级席位"
    )


class PromotionForfeitRequest(BaseModel):
    """对已冻结名单标记一位当前有效晋级者放弃。"""

    request_id: str = Field(
        min_length=1, max_length=128, description="幂等键：重复提交返回首次结果"
    )
    participant_id: int = Field(
        ge=1, description="标记放弃的参赛者（须为名单上的当前有效晋级者）"
    )
    reason: str = Field(min_length=1, max_length=10_000, description="放弃理由")


class PromotionParticipantSnapshot(BaseModel):
    """名单上一位参赛者的冻结快照：身份信息与发布时的排名依据。"""

    participant_id: int
    number: int
    name: str
    # 发布时冻结的排名与依据（排名积分、Buchholz）
    rank: int
    rank_points: float
    buchholz: float


class PromotionSeatCurrent(PromotionParticipantSnapshot):
    """席位当前持有者：source 标明来自原始冻结还是候补递补。"""

    # original（发布时冻结的原始持有者）/ waitlist（候补递补）
    source: str


class PromotionSeatResponse(BaseModel):
    seat_no: int
    # active（有当前有效晋级者）/ vacant（持有者放弃且无候补，空席）
    status: str
    # 发布时冻结的原始持有者与排名依据（不改写）
    original: PromotionParticipantSnapshot
    # 当前持有者与冻结依据；空席为 null
    current: PromotionSeatCurrent | None = None


class PromotionWaitlistEntryResponse(BaseModel):
    position: int
    participant: PromotionParticipantSnapshot
    # waiting（候补中）/ promoted（已递补）
    status: str


class PromotionListResponse(BaseModel):
    id: int
    tournament_id: int
    # 赛事内版本（从 1 递增）
    version: int
    qualifying_round_no: int
    slots: int
    expected_event_seq: int
    seats: list[PromotionSeatResponse]
    waitlist: list[PromotionWaitlistEntryResponse]
    created_at: datetime

    @field_serializer("created_at")
    def serialize_created_at(self, v: datetime) -> str:
        return _serialize_utc(v)


class PromotionForfeitResponse(BaseModel):
    """标记放弃的结果：席位、放弃者、递补者（无候补为 null，席位空置）。"""

    list_id: int
    tournament_id: int
    version: int
    seat_no: int
    # active（已由最前候补递补）/ vacant（无候补，保持空席）
    seat_status: str
    forfeited: PromotionParticipantSnapshot
    promoted: PromotionSeatCurrent | None = None
    reason: str
    created_at: datetime

    @field_serializer("created_at")
    def serialize_created_at(self, v: datetime) -> str:
        return _serialize_utc(v)
