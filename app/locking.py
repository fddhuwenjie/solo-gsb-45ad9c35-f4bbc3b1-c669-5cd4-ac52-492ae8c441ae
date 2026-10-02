"""赛事写请求的进程内串行化。

实时推送基于进程内事件总线（单进程部署，见 docker-compose.yml）；
SQLite 在 rollback journal 模式下同一时刻也只允许一个写事务，若两个
连接都先持共享锁再升级写锁会立即收到 deadlock 型 SQLITE_BUSY
（busy_timeout 不生效）。因此所有「读现状 → 写业务数据 → 原子分配
序号 → 提交 → 广播」的请求统一经过一把进程级可重入写锁：

- 同一赛事的并发创建/报名/生成轮次/赛果请求不会交错：业务校验
  （如轮次是否存在、当前编号）与序号分配对并发安全，提交失败
  （回滚）不会留下半成品与空号；
- 提交顺序与事件总线上的发布顺序严格一致：事件按 seq 升序到达
  订阅队列，SSE 客户端不乱序；
- 不同赛事的写请求也串行——与 SQLite「单写者」的实际能力一致，
  但读请求/SSE 长连接不受影响。

序号在数据库事务内仍由 EventCounter 的原子 UPSERT + 唯一约束
分配/兜底：即使绕过本锁（或未来换用多进程/PostgreSQL），序号
在数据库层依旧唯一，只是跨进程的实时发布顺序需要更强的总线。
"""
import threading
from collections.abc import Iterator
from contextlib import contextmanager

_writer_lock = threading.RLock()


@contextmanager
def writer_lock() -> Iterator[None]:
    """获取进程级写锁（可重入）。锁内必须完成提交或回滚。"""
    _writer_lock.acquire()
    try:
        yield
    finally:
        _writer_lock.release()
