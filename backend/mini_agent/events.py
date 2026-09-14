"""运行事件总线：把运行时的增量输出实时投递给该运行的 SSE 订阅者。

为什么需要它：SSE 若靠"定时轮询数据库"来发现内容变化，推送粒度就被轮询间隔锁死——
模型一秒吐几十个增量，轮询只能看到最后一次快照，前端于是变成"整段蹦出"而不是逐字生长。
这里让运行时在产生增量的当下直接投递，轮询只留作快照兜底。

三条设计约定：
- **尽力而为**：投递失败（订阅者堆积）只丢弃该条增量，不阻塞运行时；终态后的全量刷新会补齐最终内容。
- **各自消费**：每个订阅者拿到独立队列，互不影响；一个订阅者卡住不会拖慢其它订阅者。
- **无订阅者不积压**：没有订阅者时发布是空操作，不会在内存里留下没人取的数据。
"""
import asyncio
from collections import defaultdict
from typing import Any


class RunEventBus:
    """按 run_id 分频道的进程内广播器。"""

    # 单个订阅者的队列上限：够缓冲突发的增量，又能在浏览器卡死时限住内存。
    QUEUE_SIZE = 512

    def __init__(self) -> None:
        self._channels: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    def subscribe(self, run_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self.QUEUE_SIZE)
        self._channels[run_id].add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """取消订阅；频道空了就连同频道一起摘掉，避免长期运行后频道表无限增长。"""
        queues = self._channels.get(run_id)
        if not queues:
            return
        queues.discard(queue)
        if not queues:
            self._channels.pop(run_id, None)

    def publish(self, run_id: str, payload: dict[str, Any]) -> None:
        """把一条事件投给该运行的所有订阅者；没有订阅者时什么都不做。"""
        for queue in list(self._channels.get(run_id, ())):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # 订阅者消费不过来（例如浏览器暂停了页面）：丢掉这条即可，
                # 不能在这里等待——那会把整个运行拖住。
                continue
