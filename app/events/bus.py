"""进程内事件总线：把已提交的赛事事件实时扇出给 SSE 订阅者。

线程模型：FastAPI 的同步端点在线程池中执行（事件产生方），SSE
端点是 async 的（消费方，运行在主事件循环）。subscribe() 必须在
事件循环线程中调用以创建 asyncio.Queue；publish() 可从任意线程
调用，通过 lifespan 中绑定的 run_coroutine_threadsafe 投递。

消息可靠性：每个订阅队列有界。队列满时丢弃该赛事最旧的缓冲事件，
SSE 端点会通过 seq 缺口检测并回源数据库补发，因此不丢事件，只
可能短暂乱序（缺口会在推送前补齐）。
"""
import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)

# 单个 SSE 订阅者的内存缓冲上限
QUEUE_MAXSIZE = 10_000
# 队列内哨兵：缓冲溢出时插入，通知 SSE 端点回源数据库补齐缺口
_DROPPED = object()


@dataclass(frozen=True)
class EventSnapshot:
    """已提交事件的快照，供实时推送与分页接口统一序列化。"""

    tournament_id: int
    seq: int
    type: str
    payload: dict
    occurred_at: datetime


class EventBus:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: dict[
            int, set["asyncio.Queue[EventSnapshot | object]"]
        ] = defaultdict(set)

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """在应用启动时绑定主事件循环；停机时传 None 解绑。"""
        self._loop = loop

    def subscribe(
        self, tournament_id: int
    ) -> "asyncio.Queue[EventSnapshot | object]":
        """注册一个赛事的订阅队列（须在事件循环线程调用）。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._subscribers[tournament_id].add(queue)
        return queue

    def unsubscribe(self, tournament_id: int, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(tournament_id)
        if subs is not None:
            subs.discard(queue)
            if not subs:
                self._subscribers.pop(tournament_id, None)

    def subscriber_count(self, tournament_id: int) -> int:
        return len(self._subscribers.get(tournament_id, ()))

    def publish(self, snapshots: list[EventSnapshot]) -> None:
        """把已提交事件投递给对应赛事的全部订阅者（可跨线程调用）。

        无订阅者或事件循环未绑定（如测试中直接调用）时静默返回。
        """
        if not snapshots or self._loop is None:
            return
        # 按赛事分组，逐队列投递，保证单队列内顺序
        by_tournament: dict[int, list[EventSnapshot]] = defaultdict(list)
        for snap in snapshots:
            by_tournament[snap.tournament_id].append(snap)
        for tournament_id, items in by_tournament.items():
            for queue in tuple(self._subscribers.get(tournament_id, ())):
                self._deliver(queue, items)

    def _deliver(
        self,
        queue: "asyncio.Queue[EventSnapshot | object]",
        items: list[EventSnapshot],
    ) -> None:
        assert self._loop is not None

        async def _put() -> None:
            for item in items:
                try:
                    queue.put_nowait(item)
                except asyncio.QueueFull:
                    # 慢速消费者：丢弃缓冲，通知端点回源补齐并续传
                    dropped = 0
                    while not queue.empty():
                        queue.get_nowait()
                        dropped += 1
                    queue.put_nowait(_DROPPED)
                    queue.put_nowait(item)
                    logger.warning(
                        "SSE subscriber buffer overflow, dropped %d events; "
                        "client will backfill from database",
                        dropped,
                    )

        try:
            asyncio.run_coroutine_threadsafe(_put(), self._loop)
        except RuntimeError:
            # 事件循环已关闭（应用停机中）
            pass


# 进程级单例
event_bus = EventBus()
