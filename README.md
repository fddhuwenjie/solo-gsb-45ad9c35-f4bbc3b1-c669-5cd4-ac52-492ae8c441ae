# 瑞士轮赛务 API（Swiss Tournament API）

基于 **Python + FastAPI + SQLite** 的编程挑战赛瑞士轮编排服务。

- 创建赛事、批量录入参赛者；
- 一键生成下一轮：按当前积分分组，在**禁止重复交手**的前提下
  **全局最小化跨分组积分差之和**；多个最优方案按参赛者编号取
  字典序固定排序；
- 人数为奇数时，**轮空授予尚未轮空且当前排名最低的选手**（轮空计 1 分）；
- 录入每轮赛果（胜 1 / 和 0.5 / 负 0），一轮全部录完自动封盘；
- 排名查询：积分降序，同分按 **Buchholz 分**（对手积分之和）降序，
  再按编号升序；
- **轮次工位分配**：为赛事维护工位（编号 / 可用状态 / 能力标签），
  为参赛者登记必需标签；轮次生成后一键发布**版本化**工位方案——
  依次**最大化已分配对局数**、**最小化选手相对上一轮的换位次数**，
  再按 `(match_id, station_no)` 字典序固定裁决；容量不足时仍保存
  最大可行方案，并逐场说明未分配对局缺少的标签或冲突的已占工位；
- **现场报到签到快照**：下一轮尚未生成时，运营方可按目标轮次提交
  到场/缺席参赛者编号快照（携带 `request_id` 幂等键与
  `expected_event_seq` 序号快照）；两组名单必须互不相交且恰好覆盖
  当前报名名单。同一份快照可更新，版本递增并写入签到变更事件；
  确认后可再次更新但旧确认自动失效，需重新确认。生成轮次时若存在
  快照，仅到场者进入瑞士配对与轮空选择，缺席者保留历史积分/对局但
  本轮不得分；未提交快照则沿用全员编排。
- **持久化轮次计时**：轮次生成后可启动计时（`start` 设定总时长），
  支持全局 `pause` / `resume` 与对指定非轮空对局的 `extend` 加时；
  剩余时间由服务端 UTC 锚点与累计暂停时长在读取时推算，**不运行
  逐秒写库任务，容器重启后读数连续**；命令经幂等键（`request_id`）
  与乐观版本（`expected_version`）控制，晚于该场截止时刻的赛果
  拒绝录入；
- **可断线续传的赛事事件流（SSE）**：创建赛事、录入参赛者、生成轮次、
  提交赛果、发布工位方案时在同一数据库事务中写入事件；每个赛事内序号由
  `event_counters` 计数器行在事务内原子分配（`INSERT ... ON CONFLICT
  DO UPDATE ... RETURNING`），并发提交同样唯一且连续；现场大屏/解说端
  可订阅实时推送，断线后凭 `Last-Event-ID` 精确续传，并提供按序号
  分页查询的历史事件接口。
- **定向赛务公告与确认回执**：按「参赛者 / 当前排名区间 / 某版工位
  方案的工位号」三类规则取并集、去重后再应用排除名单，发布时在**同一
  写事务内确认事件序号快照未变化**并冻结收件人；规则无效或快照已
  变化一律不落库。公告正文不可改写，只能发布新版本并重新冻结收件人，
  **旧版回执不沿用**；仅该版冻结收件人可确认回执，重复提交返回原
  回执，另提供已确认 / 未确认名单查询。
- **已提交赛果的可审计复核**：可为非轮空、已提交且尚未生成下一轮的
  对局创建一条待处理复核（携带 `request_id` 幂等键与
  `expected_event_seq` 序号快照、拟更正比分与理由），创建即**冻结
  原比分**；对局双方分别同意/驳回，重复决定返回原结果、改票原地
  更新不产生额外记录；任一驳回即关闭且比分不变，双方同意时在**同一
  写事务内**更正比分、重算可见排名并写入复核关闭事件。赛事存在待
  处理复核时**禁止生成下一轮**；复核详情与赛事列表完整保留原比分、
  提议、双方决定与最终状态。
- **可追溯的扣分账本**：裁判可为参赛者登记**半分整数倍**的扣分
  （携带 `request_id` 幂等键与 `expected_event_seq` 序号快照、理由），
  也可以一条**新记录撤销**仍生效的扣分（原扣分保留并置为已撤销）；
  仅允许在**所有已生成轮次结束且没有待处理赛果复核**时登记或撤销。
  重复请求原样返回首次结果，快照冲突、重复撤销一律 `409` 不落库。
  排名保留**对局积分**，并返回**有效扣分**与**排名积分**（= 对局
  积分 − 有效扣分，**允许为负**）；排序、Buchholz（随对手的排名
  积分重算）与下一轮积分分组均以排名积分为准。账本只增不改，可按
  参赛者查询完整记录。
- **冻结式晋级名单**：在所有已生成轮次结束且没有待处理赛果复核时，
  运营方可按 `request_id` 幂等键、`expected_event_seq` 序号快照、
  **资格轮次**与**晋级名额**发布一份晋级名单——服务在**同一事务内
  重算**当时的排名积分与 Buchholz，按排名积分 → Buchholz → 参赛者
  编号固定裁决，**冻结前 N 名席位与后续候补顺序**；序号变化、名额
  非法或仍有未完成轮次一律 `409` 且不留记录。名单发布后排名与扣分
  变化**不改写该版本**；可对**当前有效晋级者**标记放弃，同一事务内
  把**最前候补**提升为该席位（无候补则保持空席），重复 `request_id`
  返回首次结果。提供赛事/版本查询，每席位含原始与当前参赛者、状态
  与冻结依据，发布与递补均写入事件流。
- **赛事维护态与归档（状态控制）**：赛事初始为 `active`，运营方可携带
  `request_id` 幂等键与 `expected_event_seq` 序号快照执行
  `pause` / `resume` / `archive`，每次变更与 `tournament.*` 事件在
  **同一事务**提交，重复 `request_id` 原样返回首次结果。`pause` 在
  同一事务**冻结所有运行中的轮次时钟**；维护期间参赛者、轮次、赛果、
  签到、工位、计时、公告、复核、扣分、晋级等**写操作统一 `409`
  且不留痕**，查询与事件续传（SSE）照常可用。`resume` **只恢复被赛事
  暂停的时钟**——手动暂停的时钟保持暂停，剩余时间不因维护流逝。
  `archive` 仅允许在 `paused` 且**所有轮次已结束、无待处理复核**时
  进入，归档后**永久只读**。

配对在 Edmonds 带花树（Blossom）最小权完美匹配算法上实现
（`networkx.min_weight_matching`，O(n³)），同代价方案用可加的
整数次级权重精确保证字典序唯一，不存在浮点误差。

## 一条命令启动

```bash
docker compose up -d --build
```

启动后：

| 用途 | 地址 |
| --- | --- |
| API 服务（健康检查） | http://localhost:8000/health |
| 交互式接口文档 Swagger UI | http://localhost:8000/docs |
| ReDoc 文档 | http://localhost:8000/redoc |

停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除赛事数据
```

## 配置

所有配置通过环境变量提供（见 `docker-compose.yml`）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:////data/swiss.db` | SQLAlchemy 数据库地址；容器内 `/data` 已挂载为命名卷 `swiss-data`，数据持久化 |
| `SSE_HEARTBEAT_SECONDS` | `15` | SSE 心跳间隔（秒）：无事件时定期发送 `: keep-alive` 注释保活 |
| `EVENTS_PAGE_LIMIT_CEILING` | `200` | 事件分页接口每页最大条数 |

端口映射在 `docker-compose.yml` 的 `ports` 中调整（默认宿主机 `8000`）。

### 本地（无 Docker）运行

```bash
pip install -r requirements.txt
export DATABASE_URL="sqlite:///./swiss.db"   # 可选
uvicorn app.main:app --reload
```

## 接口示例

### 1. 创建赛事

```bash
curl -X POST http://localhost:8000/tournaments \
  -H "Content-Type: application/json" \
  -d '{"name": "2026 秋季编程挑战赛"}'
# {"id":1,"name":"2026 秋季编程挑战赛"}
```

### 2. 录入参赛者（编号按顺序自动从 1 分配）

```bash
curl -X POST http://localhost:8000/tournaments/1/participants/bulk \
  -H "Content-Type: application/json" \
  -d '{"names": ["Alice", "Bob", "Cara", "Dan", "Eve"]}'
```

也可单个录入：`POST /tournaments/{id}/participants`，body `{"name": "Alice"}`。

> 首轮签到快照建立或首轮生成后，赛事名单即锁定，不能再增删参赛者。

### 3. 生成下一轮

```bash
curl -X POST http://localhost:8000/tournaments/1/rounds/next
```

```json
{
  "id": 1, "round_no": 1, "status": "open",
  "matches": [
    {"id": 1, "player_white": {"id": 1, "number": 1, "name": "Alice"},
               "player_black": {"id": 2, "number": 2, "name": "Bob"},
     "score_white": null, "score_black": null, "is_bye": false},
    {"id": 2, "player_white": {"id": 3, "number": 3, "name": "Cara"},
               "player_black": {"id": 4, "number": 4, "name": "Dan"},
     "score_white": null, "score_black": null, "is_bye": false},
    {"id": 3, "player_white": {"id": 5, "number": 5, "name": "Eve"},
               "player_black": null,
     "score_white": 1.0, "score_black": null, "is_bye": true}
  ]
}
```

- 首轮所有人同分为 0，按编号顺序相邻配对（字典序最小方案）；
- 奇数人的轮空场次 `is_bye=true`，轮空方自动得 1 分，无需录入；
- 上一轮未全部录完时调用会返回 `409`。

### 4. 为下一轮建立/确认现场签到快照

在第 1 轮结束、第 2 轮尚未生成时，可以先为第 2 轮建立可更新的签到
快照。`present_numbers` 与 `absent_numbers` 使用参赛者编号，两个
集合必须互不相交，并且合并后恰好等于当前报名名单：

```bash
curl -X PUT http://localhost:8000/tournaments/1/checkin-snapshots \
  -H "Content-Type: application/json" \
  -d '{
        "request_id": "round2-checkin-v1",
        "expected_event_seq": 9,
        "round_no": 2,
        "present_numbers": [1, 3, 4, 5],
        "absent_numbers": [2]
      }'
```

```json
{
  "id": 1,
  "tournament_id": 1,
  "round_no": 2,
  "version": 1,
  "present_numbers": [1, 3, 4, 5],
  "absent_numbers": [2],
  "roster": [
    {"participant_id": 1, "number": 1, "name": "Alice", "attended": true},
    {"participant_id": 2, "number": 2, "name": "Bob", "attended": false},
    {"participant_id": 3, "number": 3, "name": "Cara", "attended": true},
    {"participant_id": 4, "number": 4, "name": "Dan", "attended": true},
    {"participant_id": 5, "number": 5, "name": "Eve", "attended": true}
  ],
  "confirmed": false,
  "consumed": false,
  "confirmed_at": null,
  "created_at": "2026-10-01T09:00:00Z",
  "updated_at": "2026-10-01T09:00:00Z"
}
```

现场名单变化时，用新的 `request_id` 再次提交即可；同一轮快照保留
同一 `id`，`version` 递增。相同 `request_id` 与请求体重放会原样
返回首次结果，不产生新版本或事件。序号过期、名单重复、名单未恰好
覆盖报名名单、目标轮次不是尚未生成的下一轮，均返回 `409` 且不留
记录。

```bash
# 确认第 2 轮签到快照
curl -X POST http://localhost:8000/tournaments/1/checkin-snapshots/confirm \
  -H "Content-Type: application/json" \
  -d '{
        "request_id": "round2-checkin-confirm-v1",
        "expected_event_seq": 10,
        "round_no": 2
      }'

# 查询某轮快照，或列出全部快照
curl http://localhost:8000/tournaments/1/checkin-snapshots/2
curl http://localhost:8000/tournaments/1/checkin-snapshots
```

随后照常调用 `POST /tournaments/1/rounds/next`：若第 2 轮快照存在，
只有 `present_numbers` 中的参赛者进入瑞士配对和轮空选择；缺席者
保留此前积分与历史对局，但不会获得第 2 轮积分，也不会改变已经结束
轮次的任何数据。没有快照时，生成逻辑仍按全员编排。轮次生成后
`consumed=true`，该快照冻结，不能再更新或确认。

### 5. 录入赛果

每场得分只能取 `0`、`0.5`、`1`，双方之和必须为 `1`：

```bash
curl -X PUT http://localhost:8000/matches/1/result \
  -H "Content-Type: application/json" \
  -d '{"score_white": 1.0, "score_black": 0.0}'

# Cara 与 Dan 和棋（该轮最后一场录入后轮次自动 closed）
curl -X PUT http://localhost:8000/matches/2/result \
  -H "Content-Type: application/json" \
  -d '{"score_white": 0.5, "score_black": 0.5}'
```

### 6. 查询排名（含有效扣分、排名积分与 Buchholz 分）

```bash
curl http://localhost:8000/tournaments/1/standings
```

```json
[
  {"rank": 1, "participant_id": 1, "number": 1, "name": "Alice",
   "points": 1.0, "penalty": 0.0, "rank_points": 1.0,
   "buchholz": 0.0, "played": 1, "byes": 0},
  {"rank": 2, "participant_id": 5, "number": 5, "name": "Eve",
   "points": 1.0, "penalty": 0.0, "rank_points": 1.0,
   "buchholz": 0.0, "played": 0, "byes": 1},
  {"rank": 3, "participant_id": 3, "number": 3, "name": "Cara",
   "points": 0.5, "penalty": 0.0, "rank_points": 0.5,
   "buchholz": 0.5, "played": 1, "byes": 0},
  {"rank": 4, "participant_id": 4, "number": 4, "name": "Dan",
   "points": 0.5, "penalty": 0.0, "rank_points": 0.5,
   "buchholz": 0.5, "played": 1, "byes": 0},
  {"rank": 5, "participant_id": 2, "number": 2, "name": "Bob",
   "points": 0.0, "penalty": 0.0, "rank_points": 0.0,
   "buchholz": 1.0, "played": 1, "byes": 0}
]
```

排名规则：`rank_points`（**排名积分** = 对局积分 `points` − 当前有效
扣分 `penalty`，允许为负）降序 → `buchholz`（所有实际对阵对手的
**排名积分**之和，轮空不计对手，随对手扣分重算）降序 → `number`
升序。上例中 Alice、Eve 同为 1 分且 Buchholz 均为 0，编号小的
Alice 居首；Bob 虽 0 分，但因输给 Alice，Buchholz 为 1.0。无扣分
时 `rank_points` 与 `points` 一致、`penalty` 为 0。

### 7. 订阅赛事事件流（SSE，供现场大屏 / 解说端）

`GET /tournaments/{id}/events/stream` 以 `text/event-stream` 持续推送
该赛事的事件。服务默认监听 **8000 端口**，订阅命令：

```bash
# 从头订阅（先补发全部历史事件，再实时推送）
curl -N http://localhost:8000/tournaments/1/events/stream

# 断线续传：从序号 5 之后继续（等效于浏览器 EventSource 自动
# 携带的 Last-Event-ID: 5 请求头）
curl -N -H "Last-Event-ID: 5" \
  http://localhost:8000/tournaments/1/events/stream

# 也可以用查询参数指定起点
curl -N "http://localhost:8000/tournaments/1/events/stream?after=5"
```

每条消息的格式：

```
id: 6
event: round.generated
data: {"tournament_id": 1, "seq": 6, "type": "round.generated",
       "occurred_at": "2026-09-25T08:30:00.123456Z",
       "payload": {"round_id": 2, "round_no": 2, "status": "open",
                   "match_ids": [4, 5], "bye_match_id": 6}}
```

- `id` 即赛事内单调递增序号（每个赛事独立、从 1 开始）；浏览器
  `EventSource` 断线重连时会自动带上 `Last-Event-ID`，服务端先补发
  该序号之后的全部持久化事件，再无缝切换为实时推送——重放与实时
  切换期间不漏发、不乱序、不重复；
- `data` 为 JSON 信封：事件类型（`type`）、UTC 发生时间
  （`occurred_at`，ISO 8601）与相关资源标识（`payload`，如
  `participant_id` / `round_id` / `match_id`）；
- 无事件时定期发送 `: keep-alive` 心跳注释（间隔见
  `SSE_HEARTBEAT_SECONDS`），防止代理断开空闲连接；
- 事件类型：`tournament.created`、`participant.registered`、
  `round.generated`、`result.submitted`、`round.closed`、
  `station_plan.published`、`round_clock.started`、
  `round_clock.paused`、`round_clock.resumed`、
  `round_clock.extended`、`announcement.published`、
  `announcement.acknowledged`、`result_review.created`、
  `result_review.closed`、`penalty.recorded`、`penalty.revoked`、
  `checkin.snapshot_changed`、`checkin.snapshot_confirmed`、
  `promotion.published`、`promotion.substituted`、
  `tournament.paused`、`tournament.resumed`、`tournament.archived`；
- 事件与业务变更在**同一数据库事务**中写入：事务回滚则事件一并
  回滚，不会出现"有事件无变更"或"有变更无事件"；
- **并发安全**：事件序号不再"查询最大 seq 后插入"，而是对
  `event_counters` 计数器行做事务内原子 UPSERT——同一赛事同时提交
  参赛者、轮次或赛果变更时，数据库层裁决互斥，提交的序号唯一且
  连续（无重号、无空号），回滚不占号；进程内写请求另经一把写锁
  串行化，保证业务校验一致、"提交顺序 == 发布顺序"，且提交失败
  绝不广播；
- SSE 端点对实时事件做序号去重与缺口检测：内存中乱序/超前的事件
  会被挂起并与数据库回源结果合并，缓冲溢出则整体回源补发，
  客户端续接不漏、不重、不乱。

> 实时推送基于进程内事件总线，写请求也在进程内串行，请以单进程
> 方式运行（默认 `docker compose up` 即单容器单 worker）。序号的
> 唯一性由数据库原子计数器与唯一约束保证；多进程部署需改用跨进程
> 的事件总线（如 Redis/PostgreSQL LISTEN）。

### 8. 分页查询历史事件

```bash
# 按序号升序，每页 2 条
curl "http://localhost:8000/tournaments/1/events?after=0&limit=2"
```

```json
{
  "items": [
    {"tournament_id": 1, "seq": 1, "type": "tournament.created",
     "payload": {}, "occurred_at": "2026-09-25T08:00:00.000000Z"},
    {"tournament_id": 1, "seq": 2, "type": "participant.registered",
     "payload": {"participant_id": 1, "number": 1, "name": "Alice"},
     "occurred_at": "2026-09-25T08:01:00.000000Z"}
  ],
  "last_seq": 2,
  "has_more": true
}
```

用上一页返回的 `last_seq` 作为下一次的 `after` 即可翻页；
`has_more=false` 表示已读完全部。

### 9. 维护工位与参赛者必需标签

工位有**编号**（赛事内唯一）、**可用状态**与**能力标签**；参赛者可
登记**必需标签**。分配工位时，工位必须可用且其能力标签覆盖对局
双方选手必需标签的并集。

```bash
# 批量新增工位（也可单个 POST /tournaments/1/stations）
curl -X POST http://localhost:8000/tournaments/1/stations/bulk \
  -H "Content-Type: application/json" \
  -d '{"stations": [
        {"station_no": 1, "tags": ["直播"]},
        {"station_no": 2},
        {"station_no": 3, "available": false}
      ]}'

# 更新工位可用状态与能力标签（整体替换）
curl -X PUT http://localhost:8000/tournaments/1/stations/2 \
  -H "Content-Type: application/json" \
  -d '{"available": true, "tags": ["直播", "安静区"]}'

# 登记参赛者的必需标签（创建时也可带 required_tags 字段）
curl -X PUT http://localhost:8000/tournaments/1/participants/1/tags \
  -H "Content-Type: application/json" \
  -d '{"required_tags": ["直播"]}'

curl http://localhost:8000/tournaments/1/stations
```

标签自动去空白、去重、排序；空标签返回 `422`。工位编号重复返回
`409`。首轮生成后仍可调整工位与标签（已发布的方案是快照，不受
后续变更影响）。

### 10. 发布与查询轮次工位方案

轮次生成后，为该轮发布工位分配方案。请求携带：

- `expected_version`：期望的当前最新版本号（尚无方案时为 `0`），
  用于乐观并发控制——不一致返回 `409`，新方案版本为它加 1；
- `pins`：固定工位列表（可为空），指定某场非轮空对局必须使用的
  工位。固定工位**重复**（同一对局/同一工位出现两次）或**不兼容**
  （工位不存在、不可用、不覆盖双方必需标签，或对局为轮空/不存在）
  时返回 `409` 且不落任何数据。

```bash
curl -X POST http://localhost:8000/tournaments/1/rounds/1/station-plans \
  -H "Content-Type: application/json" \
  -d '{"expected_version": 0, "pins": [{"match_id": 2, "station_no": 2}]}'
```

```json
{
  "id": 1, "round_id": 1, "round_no": 1, "version": 1,
  "assignments": [
    {"match_id": 1, "station_no": 1},
    {"match_id": 2, "station_no": 2}
  ],
  "unassigned": [
    {"match_id": 3, "required_tags": [], "missing_tags": [],
     "conflicting_stations": [1, 2],
     "reason": "可覆盖全部必需标签的工位已被占用：1、2"}
  ],
  "created_at": "2026-09-26T08:35:00.123456Z"
}
```

上例中第 1 场对局有选手登记了 `直播` 必需标签，被分到覆盖该标签
且编号最小的 1 号工位；固定工位 `pins` 指定的第 2 场 → 2 号工位
原样保留；第 3 场因可用工位不足未分配，`unassigned` 给出原因。

求解规则（优先级从高到低，详见「工位分配规则细则」）：

1. **最大化已分配对局数**：每场非轮空对局至多占一个可用工位，
   每个工位至多承接一场对局（轮空对局不占工位）；
2. **最小化选手相对上一轮的换位次数**：以上一轮最新方案为基线，
   两轮都被分配且工位编号不同的选手计一次换位；
3. **按 `(match_id, station_no)` 字典序固定裁决**：小编号场优先
   占用小编号的可行工位，结果唯一确定。

**容量不足时仍保存最大可行方案**：`unassigned` 逐场说明原因——
`missing_tags` 为所有可用工位都不提供的必需标签；
`conflicting_stations` 为能覆盖全部必需标签、但本方案中已被
占用的工位编号。

发布成功会在同一事务中写入 `station_plan.published` 事件（载荷含
`round_id`、`round_no`、`plan_id`、`version`、`assignments` 与
`unassigned_match_ids`），SSE 订阅者与事件分页接口立即可见。

方案按版本查询（版本从 1 递增，发布后不可变）：

```bash
# 该轮全部版本
curl http://localhost:8000/tournaments/1/rounds/1/station-plans

# 指定版本
curl http://localhost:8000/tournaments/1/rounds/1/station-plans/1
```

### 11. 轮次计时（持久化时钟）

轮次生成后可为该轮启动计时。所有计时命令走同一个端点，请求体携带：

- `request_id`：幂等键。**重复提交同一 request_id 与相同参数会原样
  返回首次结果**（版本不推进、加时不叠加、事件不重复）；同一
  request_id 携带不同参数返回 `409`；
- `expected_version`：期望的当前时钟版本（未启动时为 `0`），用于
  乐观并发控制——不一致返回 `409`；每个成功的命令使版本 +1；
- `action`：`start` / `pause` / `resume` / `extend` 之一。

```bash
# start：设定轮次总时长（秒）并启动计时（每轮仅一次）
curl -X POST http://localhost:8000/tournaments/1/rounds/1/clock/actions \
  -H "Content-Type: application/json" \
  -d '{"request_id": "r1-start", "expected_version": 0,
       "action": "start", "total_seconds": 3600}'

# pause / resume：全局暂停 / 恢复（暂停冻结所有对局，恢复后延续）
curl -X POST http://localhost:8000/tournaments/1/rounds/1/clock/actions \
  -H "Content-Type: application/json" \
  -d '{"request_id": "r2-pause", "expected_version": 1, "action": "pause"}'
curl -X POST http://localhost:8000/tournaments/1/rounds/1/clock/actions \
  -H "Content-Type: application/json" \
  -d '{"request_id": "r3-resume", "expected_version": 2, "action": "resume"}'

# extend：只给指定非轮空对局加时（可叠加；运行或暂停中均可）
curl -X POST http://localhost:8000/tournaments/1/rounds/1/clock/actions \
  -H "Content-Type: application/json" \
  -d '{"request_id": "r4-extend", "expected_version": 3,
       "action": "extend", "match_id": 2, "seconds": 300}'

# 查询时钟：版本、状态与各场截止时刻 / 剩余秒数
curl http://localhost:8000/tournaments/1/rounds/1/clock
```

```json
{
  "tournament_id": 1, "round_id": 1, "round_no": 1,
  "version": 4, "status": "running",
  "total_seconds": 3600,
  "started_at": "2026-09-26T09:00:00.000000Z",
  "paused_at": null, "ended_at": null,
  "accumulated_pause_seconds": 95.2,
  "server_time": "2026-09-26T09:20:10.000000Z",
  "matches": [
    {"match_id": 1, "extra_seconds": 0,
     "deadline_at": "2026-09-26T10:01:35.200000Z",
     "remaining_seconds": 2465},
    {"match_id": 2, "extra_seconds": 300,
     "deadline_at": "2026-09-26T10:06:35.200000Z",
     "remaining_seconds": 2765}
  ]
}
```

计时规则：

1. **持久化推算**：剩余时间由 `started_at`（服务端 UTC 锚点）、
   `accumulated_pause_seconds`（累计暂停时长）与 `paused_at` /
   `ended_at` 在读取时推算，不运行逐秒写库任务；进程/容器重启后
   读数自然连续。`server_time` 为服务端当前 UTC，供客户端校准。
2. **状态机**：`idle`（未启动）→ `running` ⇄ `paused` → `ended`
   （轮次封盘时自动冻结）。`start` 每轮仅一次；`pause` 仅在运行中、
   `resume` 仅在暂停中可用；非法状态返回 `409` 且不落库。
3. **暂停与恢复**：`pause` 冻结所有对局的剩余时间（暂停中
   `deadline_at` 为 `null`，恢复后才重新确定）；`resume` 把本段
   暂停计入累计暂停后从冻结处延续。暂停中仍可 `extend`。
4. **加时**：`extend` 只作用于指定的**非轮空**对局（轮空对局 `409`，
   对局不存在或不属于该轮 `404`），`seconds` 累计叠加到该场
   `extra_seconds`；已提交赛果的对局不能再加时。
5. **赛果截止**：计时已启动（`running` / `paused`）的轮次中，晚于
   该场截止时刻提交的赛果返回 `409`；对该场 `extend` 加时后可重新
   获得提交窗口。未启动计时的轮次不做截止校验（与旧行为一致）。
6. **封盘**：轮次封盘（全部赛果录完）时时钟自动冻结为 `ended`，
   剩余时间停在封盘时刻；已结束轮次拒绝一切计时命令（`409`），
   已提交的赛果不受任何后续命令影响。
7. **事务与事件**：成功的命令与对应事件（`round_clock.started` /
   `.paused` / `.resumed` / `.extended`，载荷含 `round_id`、
   `round_no`、`version` 及命令参数）在**同一数据库事务**中提交，
   SSE 订阅者与事件分页接口立即可见；被拒绝的命令不产生事件、
   不落任何数据。

### 12. 定向赛务公告与确认回执

赛事进行中可发布**定向公告**（如检录、调台通知）。发布请求携带：

- `request_id`：幂等键，重复提交（相同参数）原样返回首次结果；
  同一 `request_id` 携带不同参数返回 `409`；
- `expected_event_seq`：发布前断言的赛事当前最大事件序号，用于
  **乐观快照**——不一致返回 `409` 且不落库（调用前可先经
  `GET /tournaments/{id}/events` 读取最新序号）；
- `body`：公告正文（该版本不可变）；
- `rules`：收件规则。

收件规则三类选择器取**并集、去重**，再应用排除名单：

| 字段 | 含义 |
| --- | --- |
| `participant_ids` | 直接指定参赛者 id |
| `rank_ranges` | 当前排名（与 `/standings` 同口径，实时计入进行中轮次）的闭区间 `{rank_from, rank_to}` |
| `station_plans` | 某轮某版工位方案中，指定工位号上对局的**双方选手**：`{round_no, version, station_nos}` |
| `exclude_participant_ids` | 排除名单（并集之后剔除） |

三类选择器至少提供一项。所有引用必须有效且属于当前赛事：参赛者
不存在/不属于本赛事、排名区间越界、轮次/工位方案版本不存在、
工位号不在该版方案中，或并集再排除后没有任何收件人，均返回
`409` 且不落库。

```bash
# 先读取当前事件序号
SEQ=$(curl -s http://localhost:8000/tournaments/1/events | python3 -c \
  "import sys,json;print(json.load(sys.stdin)['last_seq'])")

# 并集：选手3 + 当前排名前2 + 第1轮第1版方案中工位1的双方，再排除选手1
curl -X POST http://localhost:8000/tournaments/1/announcements \
  -H "Content-Type: application/json" \
  -d "{\"request_id\": \"ann-1\", \"expected_event_seq\": $SEQ,
       \"body\": \"请相关选手 10 分钟后到检录处\",
       \"rules\": {
         \"participant_ids\": [3],
         \"rank_ranges\": [{\"rank_from\": 1, \"rank_to\": 2}],
         \"station_plans\": [{\"round_no\": 1, \"version\": 1,
                             \"station_nos\": [1]}],
         \"exclude_participant_ids\": [1]
       }}"
```

```json
{
  "id": 1, "tournament_id": 1, "latest_version": 1,
  "created_at": "2026-09-27T08:00:00.000000Z",
  "latest_revision": {
    "id": 1, "announcement_id": 1, "tournament_id": 1,
    "version": 1, "body": "请相关选手 10 分钟后到检录处",
    "rules": {
      "participant_ids": [3],
      "rank_ranges": [{"rank_from": 1, "rank_to": 2}],
      "station_plans": [{"round_no": 1, "version": 1, "station_nos": [1]}],
      "exclude_participant_ids": [1]
    },
    "expected_event_seq": 9,
    "recipient_count": 3, "acknowledged_count": 0,
    "created_at": "2026-09-27T08:00:00.000000Z"
  }
}
```

收件人在发布的**同一写事务**内解析并**冻结**：之后排名变化、赛果
录入、工位方案新版本都不影响已发布版本的收件名单。发布成功写入
`announcement.published` 事件（载荷含 `announcement_id`、
`version`、冻结的 `participant_ids` 与 `expected_event_seq`）。

**公告不可改写**：需要更改时发布新版本（`version` 递增），新版本
重新按当时的规则与排名冻结收件人；旧版本与旧版回执原样保留，
**旧版回执不会沿用到新版本**。

```bash
SEQ=$(curl -s http://localhost:8000/tournaments/1/events | python3 -c \
  "import sys,json;print(json.load(sys.stdin)['last_seq'])")

curl -X POST http://localhost:8000/announcements/1/revisions \
  -H "Content-Type: application/json" \
  -d "{\"request_id\": \"ann-1-rev1\", \"expected_event_seq\": $SEQ,
       \"body\": \"地点改为 B 区检录处\",
       \"rules\": {\"participant_ids\": [2, 3]}}"
```

**确认回执**：仅该版本冻结的收件人可确认；非收件人返回 `409`，
参赛者不存在返回 `404`。首次确认返回 `201`；同一参赛者重复提交
**原样返回首次回执**（HTTP `200`，确认时间不变，不重复产生事件）。

```bash
# 选手 2 确认（201，返回含确认时间的回执）
curl -X POST \
  http://localhost:8000/announcements/1/revisions/2/acknowledgements \
  -H "Content-Type: application/json" \
  -d '{"participant_id": 2}'

# 重复提交：返回同一张回执
curl -X POST \
  http://localhost:8000/announcements/1/revisions/2/acknowledgements \
  -H "Content-Type: application/json" \
  -d '{"participant_id": 2}'

# 已确认 / 未确认名单（按参赛者编号升序）
curl http://localhost:8000/announcements/1/revisions/2/acknowledgements
```

```json
{
  "announcement_id": 1, "version": 2,
  "acknowledged": [
    {"participant_id": 2, "number": 2, "name": "Bob"}
  ],
  "pending": [
    {"participant_id": 3, "number": 3, "name": "Cara"}
  ]
}
```

确认成功写入 `announcement.acknowledged` 事件（载荷含
`announcement_id`、`version`、`participant_id`）。被拒绝的请求
（规则无效、快照冲突、非收件人确认）不产生事件、不落任何数据。

查询接口：

```bash
# 赛事下的公告（含最新版本摘要）
curl http://localhost:8000/tournaments/1/announcements
# 单个公告（含最新版本）
curl http://localhost:8000/announcements/1
# 全部版本 / 指定版本（含规则快照、冻结收件人数、已确认人数）
curl http://localhost:8000/announcements/1/revisions
curl http://localhost:8000/announcements/1/revisions/1
```

### 13. 已提交赛果的可审计复核

赛果已提交、但尚未生成下一轮时，如发现登记有误，可发起一条**双方
复核**。创建请求携带：

- `request_id`：幂等键，重复提交（相同参数）原样返回首次结果；
  同一 `request_id` 携带不同参数返回 `409`；
- `expected_event_seq`：创建前断言的赛事当前最大事件序号，用于
  **乐观快照**——不一致返回 `409` 且不落库（先经
  `GET /tournaments/{id}/events` 读取最新序号）；
- `score_white` / `score_black`：**拟更正比分**（各为 0 / 0.5 / 1，
  双方之和必须为 1）；
- `reason`：更正理由。

仅允许**非轮空、已提交赛果且尚未生成下一轮**的对局创建复核，且每场
对局同时至多有一条待处理复核，否则 `409` 且不落库。创建成功即
**冻结原比分**：待处理期间该场不能再修改赛果（`409`），赛事也
**不能生成下一轮**（`409`）。

```bash
# 先读取当前事件序号
SEQ=$(curl -s http://localhost:8000/tournaments/1/events | python3 -c \
  "import sys,json;print(json.load(sys.stdin)['last_seq'])")

# 对第 1 场对局发起复核：原记录为白胜，拟更正为黑胜
curl -X POST http://localhost:8000/matches/1/result-reviews \
  -H "Content-Type: application/json" \
  -d "{\"request_id\": \"review-1\", \"expected_event_seq\": $SEQ,
       \"score_white\": 0.0, \"score_black\": 1.0,
       \"reason\": \"记分表登记有误，实际黑方胜\"}"
```

```json
{
  "id": 1, "match_id": 1, "tournament_id": 1, "round_id": 1, "round_no": 1,
  "player_white": {"id": 1, "number": 1, "name": "Alice",
                   "required_tags": []},
  "player_black": {"id": 2, "number": 2, "name": "Bob",
                   "required_tags": []},
  "expected_event_seq": 12,
  "original_score_white": 1.0, "original_score_black": 0.0,
  "proposed_score_white": 0.0, "proposed_score_black": 1.0,
  "reason": "记分表登记有误，实际黑方胜",
  "status": "pending", "decisions": [],
  "created_at": "2026-09-29T08:00:00.000000Z", "closed_at": null
}
```

创建与 `result_review.created` 事件在同一事务写入，载荷含复核 id、
对局/轮次、冻结原比分、拟更正比分与序号快照。

**双方决定**：对局两位选手分别调用决定接口，`participant_id` 必须是
本场选手（非本场选手 `409`、参赛者不存在 `404`），`vote` 取
`approve`（同意）/ `reject`（驳回）。

- 每位选手**首次**决定返回 `201`；
- **重复提交相同决定**：原样返回复核当前结果（HTTP `200`），决定时间
  不变，不产生额外记录、不产生事件；
- 复核仍待处理时允许**改票**：原地更新该选手的决定（`200`），不产生
  额外记录；同意改驳回会立即关闭复核；
- **任一驳回**即关闭为 `rejected`，比分保持不变；
- **双方同意**时在**同一写事务内**把比分更正为拟更正值、关闭为
  `approved` 并写入 `result_review.closed` 事件；`/standings`
  始终按当前比分实时计算，事务提交后可见排名即已重算。

```bash
# 选手 1（小白方）同意
curl -X POST http://localhost:8000/result-reviews/1/decisions \
  -H "Content-Type: application/json" \
  -d '{"participant_id": 1, "vote": "approve"}'

# 选手 1 重复提交相同决定：返回原结果（200，不产生任何写入）
curl -X POST http://localhost:8000/result-reviews/1/decisions \
  -H "Content-Type: application/json" \
  -d '{"participant_id": 1, "vote": "approve"}'

# 选手 2（小黑方）同意 -> approved 关闭，比分更正为黑胜
curl -X POST http://localhost:8000/result-reviews/1/decisions \
  -H "Content-Type: application/json" \
  -d '{"participant_id": 2, "vote": "approve"}'
```

```json
{
  "id": 1, "match_id": 1, "tournament_id": 1, "round_id": 1, "round_no": 1,
  "player_white": {"id": 1, "number": 1, "name": "Alice",
                   "required_tags": []},
  "player_black": {"id": 2, "number": 2, "name": "Bob",
                   "required_tags": []},
  "expected_event_seq": 12,
  "original_score_white": 1.0, "original_score_black": 0.0,
  "proposed_score_white": 0.0, "proposed_score_black": 1.0,
  "reason": "记分表登记有误，实际黑方胜",
  "status": "approved",
  "decisions": [
    {"id": 1, "participant_id": 1, "number": 1, "name": "Alice",
     "vote": "approve",
     "decided_at": "2026-09-29T08:05:00.000000Z",
     "updated_at": "2026-09-29T08:05:00.000000Z"},
    {"id": 2, "participant_id": 2, "number": 2, "name": "Bob",
     "vote": "approve",
     "decided_at": "2026-09-29T08:06:00.000000Z",
     "updated_at": "2026-09-29T08:06:00.000000Z"}
  ],
  "created_at": "2026-09-29T08:00:00.000000Z",
  "closed_at": "2026-09-29T08:06:00.000000Z"
}
```

驳回关闭示例（任一选手驳回即可，无需另一方表态；比分不变）：

```bash
# 选手 1 驳回 -> rejected 立即关闭，原白胜比分保留
curl -X POST http://localhost:8000/result-reviews/1/decisions \
  -H "Content-Type: application/json" \
  -d '{"participant_id": 1, "vote": "reject"}'
```

关闭后：已决定选手再提交仍返回该复核原结果；未决定选手不能再表态
（`409`）。被驳回的复核不影响再次发起——关闭后可就同场用**新的
`request_id`** 重新申请（旧复核完整保留）。

**查询**（保留原比分、提议、双方决定与最终状态）：

```bash
# 复核详情
curl http://localhost:8000/result-reviews/1
# 某赛事下的全部复核（按创建时间）
curl http://localhost:8000/tournaments/1/result-reviews
```

**并发与冲突语义**：写请求经进程级写锁串行，并由数据库约束兜底——
- 同一对局并发创建复核：恰好一条成为待处理，其余 `409`；
- 同一选手对同一复核并发重复决定：只产生一条决定记录，一次 `201`，
  其余返回原结果（`200`）；
- 关闭（`approved`/`rejected`）的比分处理、复核状态与
  `result_review.closed` 事件在同一事务提交，要么全部生效要么全部
  回滚；待处理复核存在期间生成下一轮、修改该场赛果一律 `409`。

### 14. 可追溯的扣分账本

赛务争议处理中，裁判可为参赛者登记**扣分**（如迟到、违规），也可以
一条**新记录撤销**仍生效的扣分。账本只增不改：撤销不删除原扣分，
而是追加撤销记录并把原扣分置为 `revoked`，全程可追溯。

**登记扣分**：`POST /tournaments/{id}/penalties`，请求携带：

- `request_id`：幂等键（赛事内唯一），重复提交（相同参数）原样返回
  首次结果；同一 `request_id` 携带不同参数返回 `409`；
- `expected_event_seq`：登记前断言的赛事当前最大事件序号，用于
  **乐观快照**——不一致返回 `409` 且不落库（先经
  `GET /tournaments/{id}/events` 读取最新序号）；
- `participant_id`：被扣分的参赛者；
- `points`：扣分量，必须是 **0.5 的整数倍**（`0.5`、`1.0`、`1.5`…），
  否则 `422`；
- `reason`：扣分理由。

**仅允许在所有已生成轮次结束且没有待处理赛果复核时**登记或撤销，
否则 `409` 且不落库（尚未生成任何轮次时视为全部已结束，允许登记）。

```bash
# 先读取当前事件序号
SEQ=$(curl -s http://localhost:8000/tournaments/1/events | python3 -c \
  "import sys,json;print(json.load(sys.stdin)['last_seq'])")

# 给选手 3 登记 0.5 分扣分
curl -X POST http://localhost:8000/tournaments/1/penalties \
  -H "Content-Type: application/json" \
  -d "{\"request_id\": \"pen-1\", \"expected_event_seq\": $SEQ,
       \"participant_id\": 3, \"points\": 0.5,
       \"reason\": \"第 2 轮迟到 15 分钟\"}"
```

```json
{
  "id": 1, "tournament_id": 1, "participant_id": 3,
  "number": 3, "name": "Cara",
  "kind": "deduct", "points": 0.5,
  "reason": "第 2 轮迟到 15 分钟",
  "status": "active", "revokes_entry_id": null,
  "expected_event_seq": 12,
  "created_at": "2026-09-30T08:00:00.000000Z"
}
```

登记与 `penalty.recorded` 事件在**同一事务**提交（载荷含 `entry_id`、
`participant_id`、`points`、`reason` 与序号快照）。排名随即按新口径
重算：`/standings` 中该选手 `penalty=0.5`、`rank_points` 比对局积分
`points` 少 0.5（可为负），其对手的 `buchholz` 也随之按排名积分重算。

**撤销扣分**：`POST /penalties/{entry_id}/revoke`，以一条新记录撤销
一笔**仍生效**的扣分（`request_id` / `expected_event_seq` / `reason`
语义同上）：

```bash
SEQ=$(curl -s http://localhost:8000/tournaments/1/events | python3 -c \
  "import sys,json;print(json.load(sys.stdin)['last_seq'])")

curl -X POST http://localhost:8000/penalties/1/revoke \
  -H "Content-Type: application/json" \
  -d "{\"request_id\": \"pen-1-revoke\", \"expected_event_seq\": $SEQ,
       \"reason\": \"申诉成立，撤销扣分\"}"
```

```json
{
  "id": 2, "tournament_id": 1, "participant_id": 3,
  "number": 3, "name": "Cara",
  "kind": "revoke", "points": 0.5,
  "reason": "申诉成立，撤销扣分",
  "status": "applied", "revokes_entry_id": 1,
  "expected_event_seq": 13,
  "created_at": "2026-09-30T08:05:00.000000Z"
}
```

撤销记录、原扣分的状态更新（`active` → `revoked`）与
`penalty.revoked` 事件在**同一事务**提交；撤销后该笔扣分不再计入
有效扣分，排名即时重算。

**冲突示例**（均不落库、不占事件序号）：

```bash
# 快照冲突：期望序号落后/超前于当前最大序号 -> 409
curl -X POST http://localhost:8000/tournaments/1/penalties \
  -H "Content-Type: application/json" \
  -d '{"request_id": "pen-2", "expected_event_seq": 3,
       "participant_id": 3, "points": 0.5, "reason": "快照已过期"}'
# {"detail": "赛事快照已变化：当前最大事件序号为 14，请求期望 3"}

# 重复撤销：该扣分已被撤销（新 request_id 也不允许）-> 409
curl -X POST http://localhost:8000/penalties/1/revoke \
  -H "Content-Type: application/json" \
  -d '{"request_id": "pen-1-revoke-2", "expected_event_seq": 14,
       "reason": "再次撤销"}'
# {"detail": "该扣分已被撤销，不能重复撤销"}

# 撤销目标不是扣分条目（如撤销一条撤销记录）-> 409
curl -X POST http://localhost:8000/penalties/2/revoke \
  -H "Content-Type: application/json" \
  -d '{"request_id": "pen-3", "expected_event_seq": 14,
       "reason": "误操作"}'
# {"detail": "该记录不是扣分条目，不能撤销"}

# 轮次进行中 / 有待处理复核时登记 -> 409
# {"detail": "第 2 轮尚未结束，不能登记或撤销扣分"}
# {"detail": "赛事存在待处理的赛果复核，需待复核关闭后才能登记或撤销扣分"}

# 同一 request_id 携带不同参数 -> 409
# {"detail": "request_id 已被用于该赛事的其他扣分请求，请更换 request_id"}
```

**幂等重放**：重复提交同一 `request_id` 与相同参数（无论登记还是
撤销）原样返回首次结果（HTTP `201`），不产生新记录、不占事件序号。

**账本查询**：

```bash
# 赛事完整账本（按登记顺序，含已撤销的扣分与撤销记录）
curl http://localhost:8000/tournaments/1/penalties

# 按参赛者查询完整账本：逐条记录 + 当前有效扣分合计
curl http://localhost:8000/tournaments/1/participants/3/penalties
```

```json
{
  "participant_id": 3, "number": 3, "name": "Cara",
  "effective_penalty": 0.0,
  "entries": [
    {"id": 1, "kind": "deduct", "points": 0.5, "status": "revoked",
     "reason": "第 2 轮迟到 15 分钟", "revokes_entry_id": null, "...": "..."},
    {"id": 2, "kind": "revoke", "points": 0.5, "status": "applied",
     "reason": "申诉成立，撤销扣分", "revokes_entry_id": 1, "...": "..."}
  ]
}
```

**排名口径**：`points`（对局积分）不受扣分影响；`penalty` 为当前
有效扣分合计；`rank_points = points − penalty`（**允许为负**）是
排序、Buchholz 与下一轮积分分组的唯一依据——被扣分者在下一轮会
按扣分后的排名积分落入对应分组。

### 15. 冻结式晋级名单

赛事**所有已生成轮次均结束且没有待处理赛果复核**时，运营方可发布
一份**晋级名单**（赛事内版本从 1 递增）。发布请求携带：

- `request_id`：幂等键（赛事内唯一，与放弃操作共用命名空间），重复
  提交（相同参数）原样返回首次结果；同一 `request_id` 携带不同参数
  返回 `409`；
- `expected_event_seq`：发布前断言的赛事当前最大事件序号，用于
  **乐观快照**——不一致返回 `409` 且不落库；
- `qualifying_round_no`：**资格轮次**——排名积分与 Buchholz 只统计
  第 1 轮到该轮（含）的已结束轮次；轮次不存在返回 `409`；
- `slots`：**晋级名额**——冻结排名前 N 名为晋级席位，其余参赛者按
  同一次序进入候补。名额小于 1 返回 `422`，超过参赛人数返回 `409`。

发布在**同一事务内**重算当时的排名积分与 Buchholz（含当前有效扣分），
按**排名积分 → Buchholz → 参赛者编号**固定裁决并冻结：前 N 名写入
席位（`seats`），其余按序写入候补（`waitlist`）。仍有未完成轮次、
有待处理复核、资格轮次不存在、名额非法或快照冲突时拒绝且**不留任何
记录、不占事件序号**。

```bash
# 先读取当前事件序号
SEQ=$(curl -s http://localhost:8000/tournaments/1/events | python3 -c \
  "import sys,json;print(json.load(sys.stdin)['last_seq'])")

# 以第 3 轮为资格轮次发布 2 个晋级名额
curl -X POST http://localhost:8000/tournaments/1/promotions \
  -H "Content-Type: application/json" \
  -d "{\"request_id\": \"promo-1\", \"expected_event_seq\": $SEQ,
       \"qualifying_round_no\": 3, \"slots\": 2}"
```

```json
{
  "id": 1, "tournament_id": 1, "version": 1,
  "qualifying_round_no": 3, "slots": 2, "expected_event_seq": 21,
  "seats": [
    {"seat_no": 1, "status": "active",
     "original": {"participant_id": 3, "number": 3, "name": "Cara",
                  "rank": 1, "rank_points": 2.5, "buchholz": 4.0},
     "current": {"participant_id": 3, "number": 3, "name": "Cara",
                 "rank": 1, "rank_points": 2.5, "buchholz": 4.0,
                 "source": "original"}},
    {"seat_no": 2, "status": "active",
     "original": {"participant_id": 1, "number": 1, "name": "Alice",
                  "rank": 2, "rank_points": 2.0, "buchholz": 4.5},
     "current": {"participant_id": 1, "number": 1, "name": "Alice",
                 "rank": 2, "rank_points": 2.0, "buchholz": 4.5,
                 "source": "original"}}
  ],
  "waitlist": [
    {"position": 1, "status": "waiting",
     "participant": {"participant_id": 5, "number": 5, "name": "Eve",
                     "rank": 3, "rank_points": 2.0, "buchholz": 3.5}}
  ],
  "created_at": "2026-10-02T08:00:00.000000Z"
}
```

每个席位包含**原始参赛者**（发布时冻结）、**当前参赛者**、**状态**
（`active` / `vacant`）与**冻结依据**（排名、排名积分、Buchholz）；
`current.source` 标明当前持有者来自原始冻结（`original`）还是候补
递补（`waitlist`）。发布成功写入 `promotion.published` 事件（载荷含
名单版本、资格轮次、冻结的席位与候补顺序）。

**发布后冻结**：之后赛果复核更正、扣分登记/撤销、新一轮进行等任何
排名变化都**不改写已发布版本**——席位与候补的依据在发布时点冻结；
再次发布只会产生新版本（`version` 递增），旧版本原样保留。

**标记放弃**：`POST /promotion-lists/{list_id}/forfeits`，请求携带
`request_id`（幂等键）、`participant_id` 与 `reason`。**只有当前
有效晋级者**（某席位的当前持有者）可被标记放弃——候补、已放弃者或
不在名单上的参赛者返回 `409`（参赛者不存在 `404`）。放弃在**同一
事务内**把**最前候补**（顺位最小且仍在候补）提升为该席位的当前持有
者，并写入 `promotion.substituted` 事件；**无候补可递补时席位保持
空席**（`status=vacant`，`current=null`）。重复 `request_id` 原样
返回首次结果，不重复递补。

```bash
# 选手 3 放弃 -> 最前候补（选手 5）递补席位 1
curl -X POST http://localhost:8000/promotion-lists/1/forfeits \
  -H "Content-Type: application/json" \
  -d '{"request_id": "forfeit-1", "participant_id": 3,
       "reason": "个人原因退赛"}'
```

```json
{
  "list_id": 1, "tournament_id": 1, "version": 1,
  "seat_no": 1, "seat_status": "active",
  "forfeited": {"participant_id": 3, "number": 3, "name": "Cara",
                "rank": 1, "rank_points": 2.5, "buchholz": 4.0},
  "promoted": {"participant_id": 5, "number": 5, "name": "Eve",
               "rank": 3, "rank_points": 2.0, "buchholz": 3.5,
               "source": "waitlist"},
  "reason": "个人原因退赛",
  "created_at": "2026-10-02T09:00:00.000000Z"
}
```

**查询**（每席位原始/当前参赛者、状态与冻结依据，候补含顺位与状态）：

```bash
# 赛事全部版本
curl http://localhost:8000/tournaments/1/promotions
# 指定版本
curl http://localhost:8000/tournaments/1/promotions/1
```

### 16. 赛事维护态与归档（状态控制）

现场突发情况时，运营方可让整场赛事进入**维护态**（`paused`），事后
恢复（`active`）或最终**归档**（`archived`）。赛事创建即为 `active`，
状态随 `GET /tournaments/{id}` 返回。状态机：

```
        pause                resume
active ────────► paused ──────────────► active
  ▲                │
  │                │ archive（所有轮次已结束且无待处理复核）
  │                ▼
  └────────  archived（终态，永久只读）
```

切换接口：`POST /tournaments/{id}/status/actions`，请求体：

- `request_id`：幂等键（赛事内唯一）。重复提交（相同参数）原样返回
  首次结果、不产生新事件；同一 `request_id` 携带不同参数返回 `409`；
- `expected_event_seq`：切换前断言的赛事当前最大事件序号（乐观快照），
  不一致返回 `409` 且不落库；
- `action`：`pause` / `resume` / `archive`。

每次成功切换与 `tournament.paused` / `tournament.resumed` /
`tournament.archived` 事件在**同一事务**提交（事件流可见、SSE 实时
推送）；非法迁移（如 `active` 上 `resume`、`paused` 上再 `pause`、
归档前置条件不满足、已归档再变更）一律 `409` 且**不留任何记录、
不占事件序号**。

**pause：进入维护态并冻结时钟。** 同一事务内把所有**运行中**的轮次
时钟冻结为暂停（手动暂停与已封盘的时钟不动），响应 `frozen_clocks`
列出被冻结的时钟：

```bash
SEQ=$(curl -s http://localhost:8000/tournaments/1/events | python3 -c \
  "import sys,json;print(json.load(sys.stdin)['last_seq'])")

curl -X POST http://localhost:8000/tournaments/1/status/actions \
  -H "Content-Type: application/json" \
  -d "{\"request_id\": \"maint-1\", \"expected_event_seq\": $SEQ,
       \"action\": \"pause\"}"
```

```json
{
  "tournament_id": 1, "status": "paused", "previous_status": "active",
  "action": "pause", "request_id": "maint-1", "expected_event_seq": 9,
  "event_seq": 10,
  "frozen_clocks": [{"round_id": 2, "round_no": 2}],
  "resumed_clocks": [],
  "changed_at": "2026-10-03T10:20:00.000000Z"
}
```

**维护期间：写操作统一 409 且不留痕，查询与事件续传可用。** 参赛者、
轮次、赛果、签到、工位、计时、公告、复核、扣分、晋级等写请求一律
返回 `409`（不落任何记录、不占事件序号）：

```bash
curl -X POST http://localhost:8000/tournaments/1/rounds/next
# HTTP 409
# {"detail":"赛事处于维护态（paused），写操作暂不可用，待恢复后重试"}

curl -X PUT http://localhost:8000/matches/3/result \
  -H "Content-Type: application/json" \
  -d '{"score_white": 1, "score_black": 0}'
# HTTP 409
# {"detail":"赛事处于维护态（paused），写操作暂不可用，待恢复后重试"}
```

查询类接口（排名、轮次、时钟、公告、账本、晋级名单等）与事件流
（`GET /tournaments/{id}/events`、`/events/stream`，含
`Last-Event-ID` 续传）在维护期间**照常可用**。

**resume：恢复赛事，只唤醒被赛事暂停的时钟。** 被 `pause` 冻结的时钟
在同一事务内恢复运行（响应 `resumed_clocks`），维护窗口计入累计暂停
时长——**剩余时间不因维护流逝**；**手动暂停的时钟保持暂停**，仍由
计时命令自行恢复：

```bash
SEQ=$(curl -s http://localhost:8000/tournaments/1/events | python3 -c \
  "import sys,json;print(json.load(sys.stdin)['last_seq'])")

curl -X POST http://localhost:8000/tournaments/1/status/actions \
  -H "Content-Type: application/json" \
  -d "{\"request_id\": \"maint-2\", \"expected_event_seq\": $SEQ,
       \"action\": \"resume\"}"
```

```json
{
  "tournament_id": 1, "status": "active", "previous_status": "paused",
  "action": "resume", "request_id": "maint-2", "expected_event_seq": 10,
  "event_seq": 11,
  "frozen_clocks": [],
  "resumed_clocks": [{"round_id": 2, "round_no": 2}],
  "changed_at": "2026-10-03T11:00:00.000000Z"
}
```

**archive：归档，永久只读。** 仅允许在 `paused` 且**所有已生成轮次
均已结束、没有待处理赛果复核**时进入，否则 `409`：

```bash
curl -X POST http://localhost:8000/tournaments/1/status/actions \
  -H "Content-Type: application/json" \
  -d "{\"request_id\": \"maint-3\", \"expected_event_seq\": $SEQ,
       \"action\": \"archive\"}"
# 第 2 轮仍在进行时：HTTP 409
# {"detail":"第 2 轮尚未结束，不能归档赛事"}
```

归档成功后赛事永久只读：一切写操作（含再次状态切换）返回 `409`
（`{"detail":"赛事已归档（archived），永久只读，不能再执行写操作"}`），
查询与事件续传仍可用；归档请求的 `request_id` 重放仍原样返回首次结果。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/tournaments` | 创建赛事 |
| GET | `/tournaments` | 赛事列表 |
| GET | `/tournaments/{id}` | 赛事详情（含状态 active/paused/archived） |
| POST | `/tournaments/{id}/status/actions` | 切换赛事状态（pause/resume/archive，幂等 + 序号快照；pause 冻结运行中时钟，archive 后永久只读） |
| POST | `/tournaments/{id}/participants` | 录入单个参赛者（可带必需标签） |
| POST | `/tournaments/{id}/participants/bulk` | 批量录入参赛者 |
| GET | `/tournaments/{id}/participants` | 参赛者名单（按编号） |
| PUT | `/tournaments/{id}/participants/{number}/tags` | 登记/更新参赛者必需标签 |
| POST | `/tournaments/{id}/stations` | 新增工位 |
| POST | `/tournaments/{id}/stations/bulk` | 批量新增工位 |
| GET | `/tournaments/{id}/stations` | 工位列表（按编号） |
| PUT | `/tournaments/{id}/stations/{station_no}` | 更新工位可用状态与能力标签 |
| POST | `/tournaments/{id}/rounds/next` | 生成下一轮（存在签到快照时仅安排到场者） |
| PUT/POST | `/tournaments/{id}/checkin-snapshots` | 提交/更新下一轮签到快照（幂等 + 序号快照） |
| POST | `/tournaments/{id}/checkin-snapshots/confirm` | 确认下一轮签到快照（幂等 + 序号快照） |
| GET | `/tournaments/{id}/checkin-snapshots` | 赛事全部签到快照 |
| GET | `/tournaments/{id}/checkin-snapshots/{round_no}` | 查询指定轮次签到快照的版本与名单 |
| GET | `/tournaments/{id}/rounds` | 所有轮次与对阵 |
| GET | `/tournaments/{id}/rounds/{round_no}` | 指定轮次 |
| POST | `/tournaments/{id}/rounds/{round_no}/station-plans` | 发布该轮工位方案（新版本） |
| GET | `/tournaments/{id}/rounds/{round_no}/station-plans` | 该轮全部工位方案版本 |
| GET | `/tournaments/{id}/rounds/{round_no}/station-plans/{version}` | 指定版本的工位方案 |
| POST | `/tournaments/{id}/rounds/{round_no}/clock/actions` | 执行计时命令（start/pause/resume/extend，幂等 + 乐观版本） |
| GET | `/tournaments/{id}/rounds/{round_no}/clock` | 查询时钟版本、状态与各场截止时刻/剩余秒数 |
| POST | `/tournaments/{id}/announcements` | 发布定向公告（收件规则并集/排除、序号快照、冻结收件人、幂等） |
| GET | `/tournaments/{id}/announcements` | 赛事下公告列表（含最新版本摘要） |
| GET | `/announcements/{id}` | 公告详情（含最新版本） |
| POST | `/announcements/{id}/revisions` | 生成新版本并重新冻结收件人（幂等 + 序号快照） |
| GET | `/announcements/{id}/revisions` | 公告全部版本 |
| GET | `/announcements/{id}/revisions/{version}` | 指定版本详情（规则快照、收件/已确认人数） |
| POST | `/announcements/{id}/revisions/{version}/acknowledgements` | 该版收件人确认回执（重复提交返回原回执） |
| GET | `/announcements/{id}/revisions/{version}/acknowledgements` | 已确认 / 未确认名单 |
| POST | `/matches/{match_id}/result-reviews` | 创建赛果复核（冻结原比分、幂等 + 序号快照） |
| POST | `/result-reviews/{id}/decisions` | 本场选手同意/驳回（重复决定返回原结果、改票不产生额外记录） |
| GET | `/result-reviews/{id}` | 复核详情（原比分、提议、决定、最终状态） |
| GET | `/tournaments/{id}/result-reviews` | 赛事下的复核列表 |
| POST | `/tournaments/{id}/penalties` | 登记扣分（半分整数倍、幂等 + 序号快照；须全部轮次结束且无待处理复核） |
| POST | `/penalties/{entry_id}/revoke` | 以新记录撤销一笔仍生效的扣分（幂等 + 序号快照；重复撤销 409） |
| GET | `/tournaments/{id}/penalties` | 赛事完整扣分账本（按登记顺序） |
| GET | `/tournaments/{id}/participants/{pid}/penalties` | 参赛者完整账本（逐条记录 + 当前有效扣分合计） |
| POST | `/tournaments/{id}/promotions` | 发布晋级名单（幂等 + 序号快照；冻结前 N 名席位与候补顺序） |
| POST | `/promotion-lists/{id}/forfeits` | 标记当前有效晋级者放弃（幂等；最前候补递补，无候补则空席） |
| GET | `/tournaments/{id}/promotions` | 赛事全部晋级名单版本（席位原始/当前参赛者、状态与依据） |
| GET | `/tournaments/{id}/promotions/{version}` | 指定版本晋级名单 |
| PUT | `/matches/{match_id}/result` | 录入/修改一场赛果 |
| GET | `/tournaments/{id}/standings` | 排名（对局积分、有效扣分、排名积分与 Buchholz 分） |
| GET | `/tournaments/{id}/events/stream` | SSE 事件流（支持 `Last-Event-ID` 续传、心跳注释） |
| GET | `/tournaments/{id}/events` | 事件分页查询（`after` / `limit`） |
| GET | `/health` | 健康检查 |

## 配对规则细则

1. 每个积分分组成员优先组内配对；当组内无法消化（人数奇偶、
   重复交手禁忌）时产生跨组配对，算法在所有可行完美匹配中
   最小化跨组积分差之和（半分为最小单位）。
2. 任何两位选手至多交手一次；若因禁忌过多已无可行匹配，
   生成轮次返回 `409` 并说明原因。
3. 多个最优方案的固定排序：把配对表示为 `(小编号, 大编号)`
   并按小编号升序排列，取该配对元组的字典序最小者——等价于
   「编号最小的选手优先搭配编号尽可能小的可行搭档，依次类推」。
4. 奇数人轮空：按当前排名（积分 → Buchholz → 编号）取排名最低
   且尚未轮空者；全部都轮空过时再取排名最低者。
5. 目标轮存在签到快照时，只对到场者执行上述排名、轮空与配对；
   缺席者不进入算法、本轮不得分，历史轮次和历史对阵保持不变。
   到场人数为奇数时轮空从到场者中选择；无人到场时该轮立即封盘且
   不生成任何对局。未提交签到快照时仍按全员编排。

## 工位分配规则细则

1. 可行约束：每场非轮空对局至多占一个**可用**工位，每个工位
   至多承接一场对局；工位能力标签必须**覆盖双方选手必需标签的
   并集**。轮空对局不参与分配。请求中的固定工位（`pins`）视为
   硬约束，求解前校验，重复或不兼容直接 `409` 且不落库。
2. 优化目标按优先级依次满足：先**最大化已分配对局数**；再
   **最小化选手相对上一轮最新方案的换位次数**（仅统计两轮都被
   分配且工位编号不同的选手）；最后按 `(match_id, station_no)`
   字典序固定裁决——把分配表示为按 `match_id` 升序的
   `(match_id, station_no)` 序列，取字典序最小者。
3. 实现：在「对局 × 可用工位」二分图上求最大权匹配
   （`networkx.max_weight_matching`）。三级目标用大整数权重精确
   编码（与瑞士轮配对同一思路），权值唯一确定
   （分配数, 换位数, 逐场工位选择），最优解唯一，不存在浮点误差，
   多次发布同一版本基线结果一致。
4. 容量不足时不报错：保存当前约束下的最大可行方案，
   `unassigned` 逐场给出 `missing_tags`（所有可用工位都不提供的
   必需标签）与 `conflicting_stations`（能覆盖全部必需标签但被
   占用的工位），便于现场增补工位或调整标签后发布下一版。
5. 方案版本化：每轮版本从 1 递增、发布后不可变；`expected_version`
   提供乐观并发控制，发布成功写入 `station_plan.published` 事件。
   已发布方案保存工位编号与未分配说明的快照，后续工位/标签变更
   不影响历史版本。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

包含：核心算法对暴力枚举的随机对拍（450+ 组）、字典序最优性的
独立序贯校验，端到端 API 流程（含轮空轮换、重复对阵检测、
赛果校验、自动封盘），工位模块专项（工位维护、必需标签登记、
固定工位校验 409 不落库、最多分配/最少换位/字典序裁决、容量
不足的部分方案与原因说明、版本化查询与事件发布），轮次计时专项
（状态机非法转换 409 不落库、版本冲突、request_id 幂等重放与
冲突、暂停冻结/恢复延续、读取时推算无需写库、extend 目标校验、
截止时刻拒绝赛果与加时重开窗口、封盘冻结与命令拒绝、事件同事务
有序发布——时间断言经注入固定时钟完成，不依赖真实等待），
定向公告专项（三类收件规则并集去重与排除、按当前排名取人、
工位号取对局双方、规则无效/跨赛事引用/快照冲突 409 不落库不占号、
收件人冻结不受后续排名变化影响、request_id 幂等重放与冲突、
版本修订重新冻结且旧版回执不沿用、仅收件人可确认且重复提交返回
原回执、已确认/未确认名单、同快照并发发布仅一成一败、
发布与确认写入事件流），赛果复核专项（非轮空/已提交/未生成
下一轮/唯一待处理的创建资格、序号快照冲突 409 不落库不占号、
request_id 幂等重放与冲突、待处理期间冻结赛果且禁止生成下一轮、
仅本场选手可决定、重复决定返回原结果且改票原地更新、任一驳回
关闭比分不变、双方同意同事务更正比分并重算排名、关闭事件与
原比分/提议/决定/状态的完整保留、并发创建仅一成一败与并发重复
决定只产生一条记录），扣分账本专项（半分整数倍校验、轮次进行中/
有待处理复核时禁止登记或撤销、序号快照冲突 409 不落库不占号、
request_id 幂等重放与冲突、撤销同事务置原扣分为已撤销、重复撤销
与撤销非扣分条目 409、排名积分可为负且排序/Buchholz/下一轮分组
均以排名积分为准、按参赛者与赛事的完整账本查询、并发撤销同一
扣分仅一成一败、并发同键不同参数登记仅一成落库），晋级名单专项
（轮次未结束/有待处理复核/资格轮次不存在/名额非法/快照冲突 409
不落库不占号、资格轮次口径只统计该轮之前、固定裁决冻结席位与候补、
发布后扣分与排名变化不改写已发布版本、request_id 幂等重放与冲突、
仅当前有效晋级者可放弃且最前候补同事务递补、无候补保持空席、
递补者再放弃链式递补、名单冻结后赛事继续仍可放弃、发布与递补写入
事件流、并发放弃同一席位仅一成一败、并发同键不同参数发布仅一成
落库），赛事状态控制专项（初始 active、pause/resume/archive 状态机
非法迁移 409 不落库不占号、request_id 幂等重放与冲突、序号快照冲突、
pause 同事务冻结运行中时钟、resume 只恢复赛事暂停的时钟而手动暂停
保持暂停、维护窗口不计入剩余时间、维护期间各类写操作统一 409 不留痕
而查询与 SSE 续传可用、archive 须有 paused 且轮次全部结束无待处理
复核、归档后永久只读、并发切换仅一成一败——时间断言经注入固定时钟
完成），以及
事件流专项测试——事务回滚不留事件、
提交失败不广播且不留事件、原子计数器回滚不占号、并发报名/赛果/
开轮（多线程屏障同时放行）下编号与序号唯一连续、并发提交按序号
有序发布、断线续传、重放/实时切换竞态（不漏/不乱/不重）、实时
乱序缺口的挂起-回源合并、心跳、缓冲溢出回源、赛事隔离与分页查询
（SSE 用例通过线程内真实 uvicorn 服务器消费）。
