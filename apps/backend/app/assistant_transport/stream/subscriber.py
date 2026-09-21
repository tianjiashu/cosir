"""进程内 bounded snapshot frame 订阅者。"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field

from app.assistant_transport.stream.transport_frame import TransportFrame


class SubscriberClosed(RuntimeError):
    """当 task 删除或进程关闭导致 subscriber 关闭时抛出。"""


@dataclass(eq=False)
class Subscriber:
    """绑定事件循环、带控制优先级和有限 mutation 队列的订阅者。

    ``offer`` 只在 subscriber 所属 event loop 中执行。普通 mutation 可以合并相邻的
    ``append-text``；队列溢出时丢弃尚未发送的 mutation，并放入一个控制 frame，让客户端
    通过 attach 重新取得 full state。控制 frame 始终优先于普通 mutation，terminal/full
    frame 不会被普通 mutation 饥饿。
    """

    loop: asyncio.AbstractEventLoop
    mutation_limit: int = 64
    _mutation_queue: deque[TransportFrame] = field(default_factory=deque, init=False)
    _control_frame: TransportFrame | None = field(default=None, init=False)
    _wake: asyncio.Event = field(init=False)
    _closed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        """创建由所属 loop 持有的唤醒原语。"""

        self._wake = asyncio.Event()

    def offer(self, frame: TransportFrame) -> None:
        """从 subscriber 所属 event-loop 线程投递一帧。

        本方法本身故意不做线程安全保护；状态服务通过 ``loop.call_soon_threadsafe`` 调度它。
        已关闭的 subscriber 会静默丢弃帧。
        """

        if self._closed:
            return
        if frame.kind != "mutation":
            # full/control 帧是一个新的投递边界。排队的 mutation 产生于它之前，不能在 full
            # 状态之后被重放。
            self._mutation_queue.clear()
            self._control_frame = frame
            self._wake.set()
            return
        if self._control_frame is not None:
            return
        if self._mutation_queue and _can_coalesce(self._mutation_queue[-1], frame):
            previous = self._mutation_queue.pop()
            self._mutation_queue.append(_coalesce(previous, frame))
            self._wake.set()
            return
        if len(self._mutation_queue) >= self.mutation_limit:
            self._mutation_queue.clear()
            self._control_frame = TransportFrame(
                task_id=frame.task_id,
                kind="resync_required",
                mutations=(),
                current_run_id=frame.current_run_id,
                current_run_status=frame.current_run_status,
                resync_reason="subscriber_backpressure",
            )
            self._wake.set()
            return
        self._mutation_queue.append(frame)
        self._wake.set()

    async def get(self) -> TransportFrame:
        """等待并返回下一个 control-or-mutation 帧。"""

        while True:
            if self._closed:
                raise SubscriberClosed
            if self._control_frame is not None:
                frame = self._control_frame
                self._control_frame = None
                if not self._mutation_queue:
                    self._wake.clear()
                return frame
            if self._mutation_queue:
                frame = self._mutation_queue.popleft()
                if not self._mutation_queue:
                    self._wake.clear()
                return frame
            await self._wake.wait()

    def close(self) -> None:
        """停止投递并唤醒正在等待的消费者。"""

        self._closed = True
        self._mutation_queue.clear()
        self._control_frame = None
        self._wake.set()


def _can_coalesce(previous: TransportFrame, current: TransportFrame) -> bool:
    """返回相邻两帧是否各含一个可安全合并的 append-text 变更。"""

    if len(previous.mutations) != 1 or len(current.mutations) != 1:
        return False
    left, right = previous.mutations[0], current.mutations[0]
    return (
        left.kind == "append-text"
        and right.kind == "append-text"
        and left.path == right.path
        and previous.task_id == current.task_id
        and previous.source_run_id == current.source_run_id
        and previous.current_run_id == current.current_run_id
        and previous.current_run_status == current.current_run_status
    )


def _coalesce(previous: TransportFrame, current: TransportFrame) -> TransportFrame:
    """合并相邻的 append-text 帧，且不物化 full state。"""

    left, right = previous.mutations[0], current.mutations[0]
    return TransportFrame(
        task_id=current.task_id,
        kind="mutation",
        mutations=(
            type(left)(
                kind="append-text",
                path=left.path,
                value=f"{left.value}{right.value}",
            ),
        ),
        source_run_id=current.source_run_id,
        current_run_id=current.current_run_id,
        current_run_status=current.current_run_status,
    )


__all__ = ["Subscriber", "SubscriberClosed"]
