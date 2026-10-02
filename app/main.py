import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .database import init_db
from .events.bus import event_bus
from .routers import (
    announcements,
    checkins,
    clock,
    events,
    penalties,
    promotions,
    result_reviews,
    rounds,
    stations,
    tournaments,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 实时事件总线绑定主事件循环：同步端点线程由此向 SSE 订阅者投递
    event_bus.bind_loop(asyncio.get_running_loop())
    init_db()
    yield
    event_bus.bind_loop(None)


app = FastAPI(
    title="瑞士轮赛务 API",
    description=(
        "编程挑战赛瑞士轮编排服务：创建赛事、录入参赛者与每轮赛果，"
        "按积分分组生成下一轮（禁止重复交手、全局最小化跨组积分差、"
        "奇数人轮空给未轮空且排名最低者），并支持为尚未生成的下一轮"
        "维护版本化现场签到快照：到场者参与配对/轮空，缺席者保留历史"
        "但本轮不得分；未提交快照时按全员编排。"
        "支持维护赛事工位（编号/可用状态/能力标签）与参赛者必需标签，"
        "为每一轮发布版本化工位分配方案（最多分配 → 最少换位 → "
        "固定裁决，容量不足时保存最大可行方案并说明原因）。"
        "支持持久化轮次计时：start/pause/resume/extend 命令经幂等键与"
        "乐观版本控制落库，剩余时间由服务端 UTC 锚点与累计暂停时长推算，"
        "重启后读数连续；晚于该场截止时刻的赛果拒绝录入。"
        "支持定向赛务公告：按参赛者、当前排名区间与某版工位方案工位号的"
        "并集规则（再应用排除名单）冻结收件人，发布在同一写事务内校验"
        "事件序号快照；公告版本化、旧版回执不沿用，收件人按版本确认回执。"
        "支持已提交赛果的可审计复核：为非轮空、已提交且尚未生成下一轮的"
        "对局创建待处理复核（冻结原比分、幂等键与序号快照），双方选手"
        "分别同意/驳回（重复决定返回原结果、改票不产生额外记录），任一"
        "驳回即关闭且比分不变，双方同意在同一事务内更正比分、重算可见"
        "排名并写入关闭事件；有待处理复核时禁止生成下一轮。"
        "支持可追溯的扣分账本：裁判按幂等键与事件序号快照为参赛者登记"
        "半分整数倍的扣分（仅允许在所有已生成轮次结束且无待处理复核时），"
        "可以新记录撤销一笔仍生效的扣分；排名保留对局积分并返回有效扣分"
        "与排名积分（可为负），排序、Buchholz 与下一轮积分分组均以排名"
        "积分为准，账本可按参赛者完整查询。"
        "支持冻结式晋级名单：在所有已生成轮次结束且无待处理复核时，按"
        "幂等键、事件序号快照、资格轮次与晋级名额发布名单——同一事务内"
        "重算排名积分与 Buchholz，按排名积分、Buchholz、参赛者编号固定"
        "裁决，冻结前 N 名席位与候补顺序；发布后排名与扣分变化不改写该"
        "版本。可对当前有效晋级者标记放弃，同一事务内以最前候补递补该"
        "席位（无候补则空席），并提供赛事/版本查询。"
        "另提供每赛事独立序号、可断线续传的 SSE 赛事事件流。"
    ),
    version="1.8.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(tournaments.router)
app.include_router(rounds.router)
app.include_router(checkins.router)
app.include_router(stations.router)
app.include_router(clock.router)
app.include_router(announcements.router)
app.include_router(result_reviews.router)
app.include_router(penalties.router)
app.include_router(promotions.router)
app.include_router(events.router)
