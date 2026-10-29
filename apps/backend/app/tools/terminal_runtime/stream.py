"""每会话交互通道（terminal-v2）的输出缓冲、订阅与 wire 投影；不拥有会话生命周期。

线程到 asyncio 的桥接：worker 读线程（或任意调用线程）在会话锁内 ``publish``，订阅经
``loop.call_soon_threadsafe`` 把事件放进绑定事件循环的 ``asyncio.Queue``，WS 协程直接 ``await``，
没有线程池轮询。背压在发布侧按帧数与原始字节数计量，溢出后只投递一次 ``subscriber_overflow``，
由 WS 转为 ``resync_required``；慢客户端不阻塞 PTY 读线程，也不影响其他订阅。

输出在 worker 边界解码校验一次，这里保存已知字节数，不反复解码 Base64。
``TerminalStreamBuffer`` 的访问由所属会话锁串行化；订阅计数各自加锁。
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections import deque
from dataclasses import dataclass

from app.config.constant import Constant
from app.config.logging.logger import log
from app.tools.terminal_runtime.errors import TerminalSessionResyncRequiredError

StreamEvent = dict[str, object]
PROTOCOL = "terminal-v2"
OVERFLOW_EVENT_TYPE = "subscriber_overflow"
_METADATA_SIZE = 128


@dataclass(frozen=True)
class _Delivery:
    event: StreamEvent
    size: int


class TerminalStreamSubscription:
    """绑定一个事件循环的有界订阅；溢出只通知重连，不影响其他读取者。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, session_id: str) -> None:
        self._loop = loop
        self._session_id = session_id
        self._queue: asyncio.Queue[_Delivery] = asyncio.Queue()
        self._lock = threading.Lock()
        self._frames = 0
        self._bytes = 0
        self._closed = False
        self._overflowed = False

    def publish(self, event: StreamEvent, size: int = _METADATA_SIZE) -> None:
        """任意线程调用；输出按原始字节数计费，元数据按固定开销计费。"""
        with self._lock:
            if self._closed or self._overflowed:
                return
            if (
                self._frames + 1 > Constant.Terminal.MAX_STREAM_QUEUE_FRAMES
                or self._bytes + size > Constant.Terminal.MAX_STREAM_QUEUE_BYTES
            ):
                self._overflowed = True
                delivery = _Delivery({"type": OVERFLOW_EVENT_TYPE}, 0)
            else:
                self._frames += 1
                self._bytes += size
                delivery = _Delivery(event, size)
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, delivery)
        except RuntimeError:
            # 事件循环已关闭：订阅方已不存在，在订阅边界收敛，不向 PTY 读线程抛出。
            with self._lock:
                self._closed = True
            log.info(
                "terminal_stream_loop_closed",
                extra={
                    "msg": "终端订阅的事件循环已关闭，停止投递",
                    "data": {"session_id": self._session_id},
                },
            )

    async def get(self) -> StreamEvent:
        delivery = await self._queue.get()
        with self._lock:
            self._frames = max(0, self._frames - (1 if delivery.size else 0))
            self._bytes = max(0, self._bytes - delivery.size)
        return delivery.event

    def close(self) -> None:
        with self._lock:
            self._closed = True


@dataclass(frozen=True)
class TerminalStreamAttachment:
    attached: StreamEvent
    replay: tuple[StreamEvent, ...]
    terminal_event: StreamEvent | None
    subscription: TerminalStreamSubscription


@dataclass(frozen=True)
class _OutputFrame:
    seq: int
    encoded: str
    size: int


class TerminalStreamBuffer:
    """原始输出窗口与订阅集合；所有方法由所属会话锁保护。"""

    def __init__(self, session_id: str, limit: int) -> None:
        self._session_id = session_id
        # generation 区分同一 session_id 的输出窗口实例，客户端据此判断 after_seq 是否仍可续接。
        self._identity: StreamEvent = {
            "protocol": PROTOCOL,
            "session_id": session_id,
            "generation": f"gen_{uuid.uuid4().hex}",
        }
        self._limit = limit
        self._frames: deque[_OutputFrame] = deque()
        self._bytes = 0
        self._next_seq = 1
        self._subscribers: set[TerminalStreamSubscription] = set()
        # ``finish`` 与会话终态迁移在同一会话锁内发生：之后的 attach 只回放并返回 exit，
        # 不再登记订阅。
        self._finished = False

    @property
    def first_available_seq(self) -> int:
        return self._frames[0].seq if self._frames else self._next_seq

    @property
    def next_seq(self) -> int:
        return self._next_seq

    def append(self, encoded: str, size: int) -> None:
        frame = _OutputFrame(self._next_seq, encoded, size)
        self._next_seq += 1
        self._frames.append(frame)
        self._bytes += size
        while self._bytes > self._limit and self._frames:
            self._bytes -= self._frames.popleft().size
        event = self._output_event(frame)
        for subscriber in self._subscribers:
            subscriber.publish(event, size)

    def publish(self, event_type: str, **fields: object) -> None:
        """推送尺寸、输入标记等与画面绑定的帧，和输出同通道保证顺序。

        输入租约、交接、退出码属于会话事实，只经 snapshot ``terminal_sessions`` 投影，不在 WS 上
        重复推送：两条通道之间没有顺序保证，双推会让前端合并出过期值。
        """
        event = {**self._identity, "type": event_type, **fields}
        for subscriber in self._subscribers:
            subscriber.publish(event)

    def attach(
        self,
        after_seq: int | None,
        loop: asyncio.AbstractEventLoop,
        *,
        status: str,
        rows: int,
        columns: int,
    ) -> TerminalStreamAttachment:
        """after_seq 类型由 API 校验；这里只判断本窗口是否仍能连续回放。

        attach 与后续发布在同一会话锁内衔接：先生成 replay，再登记订阅，此后的事件全部进入
        订阅队列，WS 先发 replay 再消费队列即保证顺序。
        """
        if after_seq is not None and after_seq < self.first_available_seq - 1:
            raise TerminalSessionResyncRequiredError(after_seq, self.first_available_seq)
        subscription = TerminalStreamSubscription(loop, self._session_id)
        replay = tuple(
            self._output_event(frame)
            for frame in self._frames
            if after_seq is None or frame.seq > after_seq
        )
        attached = {
            **self._identity,
            "type": "attached",
            "first_available_seq": self.first_available_seq,
            "next_seq": self._next_seq,
            "status": status,
            "rows": rows,
            "columns": columns,
        }
        if not self._finished:
            self._subscribers.add(subscription)
        return TerminalStreamAttachment(
            attached,
            replay,
            self._exit_event() if self._finished else None,
            subscription,
        )

    def detach(self, subscription: TerminalStreamSubscription) -> None:
        self._subscribers.discard(subscription)

    def finish(self) -> None:
        self._finished = True
        event = self._exit_event()
        for subscriber in self._subscribers:
            subscriber.publish(event)
        self._subscribers.clear()

    def resync_event(self, after_seq: int | None) -> StreamEvent:
        return {
            **self._identity,
            "type": "resync_required",
            "after_seq": after_seq,
            "first_available_seq": self.first_available_seq,
            "next_seq": self._next_seq,
        }

    def _output_event(self, frame: _OutputFrame) -> StreamEvent:
        return {**self._identity, "type": "output", "seq": frame.seq, "data_base64": frame.encoded}

    def _exit_event(self) -> StreamEvent:
        """只标记流结束；终态与退出码由 snapshot 投影。"""
        return {**self._identity, "type": "exit"}
