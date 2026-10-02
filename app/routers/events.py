"""赛事事件流接口。

- GET /tournaments/{id}/events/stream：SSE 订阅。
  * 支持 Last-Event-ID 请求头（浏览器 EventSource 自动携带）或
    ?after= 查询参数（头优先），连接后先补发该序号之后的全部
    持久化事件，再无缝切换到实时推送；
  * 序号去重 + 缺口回源，保证不漏发、不乱序、不重复；
  * 无消息时定期发送 ":" 心跳注释。
- GET /tournaments/{id}/events：按序号升序分页查询持久化事件。
"""
import asyncio
import heapq
import itertools
import json
from datetime import timezone
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import config
from ..database import SessionLocal, get_db
from ..models import Event
from ..schemas import EventPageResponse, EventResponse
from ..events.bus import EventSnapshot, event_bus
from ..events.bus import _DROPPED as BUFFER_DROPPED
from ..events.service import fetch_events_after
from .tournaments import get_tournament_or_404

router = APIRouter()

# 补发阶段每次从数据库读取的批量大小
REPLAY_BATCH = 500


def _envelope(obj: Event | EventSnapshot) -> dict:
    occurred_at = obj.occurred_at
    if occurred_at.tzinfo is None:  # 存储为 naive UTC
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    return {
        "tournament_id": obj.tournament_id,
        "seq": obj.seq,
        "type": obj.type,
        "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
        "payload": obj.payload,
    }


def _format_sse(obj: Event | EventSnapshot) -> str:
    data = json.dumps(_envelope(obj), ensure_ascii=False)
    # id 供客户端重连时通过 Last-Event-ID 续传；event 为事件类型
    return f"id: {obj.seq}\nevent: {obj.type}\ndata: {data}\n\n"


def _parse_after(last_event_id: str | None, after: int) -> int:
    if last_event_id is None or last_event_id == "":
        return after
    try:
        value = int(last_event_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Last-Event-ID 必须是非负整数")
    if value < 0:
        raise HTTPException(status_code=400, detail="Last-Event-ID 必须是非负整数")
    return value


def _fetch_replay_batch(tournament_id: int, after_seq: int) -> list[Event]:
    """短事务读取补发批次（独立连接，长连接期间不占用请求级会话）。"""
    db = SessionLocal()
    try:
        return fetch_events_after(db, tournament_id, after_seq, REPLAY_BATCH)
    finally:
        db.close()


@router.get("/tournaments/{tournament_id}/events/stream")
async def stream_events(
    tournament_id: int,
    after: int = Query(0, ge=0, description="只推送该序号之后的事件"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    db: Session = Depends(get_db),
):
    # 赛事不存在直接 404，不建立长连接
    get_tournament_or_404(db, tournament_id)
    start_seq = _parse_after(last_event_id, after)

    async def generate() -> AsyncIterator[str]:
        # 关键顺序：先订阅实时队列，再读数据库补发。订阅之后提交的
        # 事件一定进入队列；补发阶段提交的事件则可能同时出现在
        # 数据库与队列中，推送时按 seq 去重，切换无缝且不漏不重。
        queue = event_bus.subscribe(tournament_id)
        current = start_seq
        # 已从实时队列取出但暂时超前（缺口/乱序）的事件不丢弃，
        # 按 seq 挂入 pending 小顶堆：缺口可能由数据库回源补齐，
        # 也可能由后续到达的实时事件补齐——两种来源合并按序输出。
        pending: list[tuple[int, int, EventSnapshot]] = []
        tiebreak = itertools.count()
        # 首次进入必须先回源补发历史；数据库读空一次后视为追平，
        # 此后仅在存在缺口（或缓冲溢出）时才查询数据库。
        caught_up = False

        def push_pending(item: EventSnapshot) -> None:
            heapq.heappush(pending, (item.seq, next(tiebreak), item))

        def drain_queue_nowait() -> bool:
            """非阻塞排空当前已缓冲的实时事件。

            seq 已推送过的重复事件直接丢弃；其余入 pending 堆。
            返回是否出现过缓冲溢出哨兵。
            """
            overflowed = False
            while not queue.empty():
                nxt = queue.get_nowait()
                if nxt is BUFFER_DROPPED:
                    overflowed = True
                elif nxt.seq > current:
                    push_pending(nxt)
            return overflowed

        try:
            yield f": connected after={current}\n\n"
            while True:
                # 1) 先取走队列中已缓冲的实时事件（含溢出标记）
                overflowed = drain_queue_nowait()

                # 2) 尚未追平 / 有缺口 / 缓冲溢出：回源数据库，
                #    回源与实时事件按 seq 去重合并（先回源、再消费
                #    pending，避免纯实时乱序时多余的数据库往返）
                need_fetch = (
                    not caught_up
                    or overflowed
                    or bool(pending and pending[0][0] > current + 1)
                )
                if need_fetch:
                    rows = await run_in_threadpool(
                        _fetch_replay_batch, tournament_id, current
                    )
                    if rows:
                        for event in rows:
                            yield _format_sse(event)
                        current = rows[-1].seq
                        continue  # 可能还有下一页，或 pending 已被补齐
                    caught_up = True

                # 3) 数据库追平后，消费 pending 中从 current+1 起连续
                #    的事件（含纯实时乱序补齐的情况）；seq<=current
                #    的为回源重叠，去重丢弃
                progressed = False
                while pending:
                    seq, _, item = pending[0]
                    if seq <= current:
                        heapq.heappop(pending)
                        continue
                    if seq != current + 1:
                        break
                    heapq.heappop(pending)
                    yield _format_sse(item)
                    current = seq
                    progressed = True
                if progressed:
                    # 连续输出期间队列可能又到了后续事件，回到顶部
                    # 重新排空/检测
                    continue

                # 4) 数据库已追平：等待下一条实时事件，超时发心跳。
                #    若仍有内存缺口，等待后回到顶部重新回源探测。
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=config.SSE_HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    if pending:
                        continue  # 缺口前序可能刚提交入库
                    yield ": keep-alive\n\n"
                    continue

                if item is BUFFER_DROPPED:
                    # 缓冲溢出：内存态不可信，回到顶部回源按序补齐
                    continue
                if item.seq <= current:
                    # 重放/实时边界竞态产生的重复事件，丢弃
                    continue
                push_pending(item)
        finally:
            event_bus.unsubscribe(tournament_id, queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/tournaments/{tournament_id}/events",
    response_model=EventPageResponse,
)
def list_events(
    tournament_id: int,
    after: int = Query(0, ge=0, description="返回序号大于该值的事件"),
    limit: int = Query(
        100, ge=1, le=config.EVENTS_PAGE_LIMIT_CEILING, description="每页条数"
    ),
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)
    # 多取 1 条判断是否还有下一页
    rows = fetch_events_after(db, tournament_id, after, limit + 1)
    has_more = len(rows) > limit
    page = rows[:limit]
    return EventPageResponse(
        items=[EventResponse.model_validate(e) for e in page],
        last_seq=page[-1].seq if page else after,
        has_more=has_more,
    )
