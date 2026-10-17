"""只读预览的缓冲、订阅和 wire 投影；不拥有会话生命周期。

输出在 worker 边界解码校验一次，此处保存已知字节数，不反复解码 Base64。
PreviewBuffer 的访问由所属会话锁串行化；各订阅队列独立加锁，慢客户端不阻塞 PTY。
"""

from __future__ import annotations

import queue
import threading
from collections import deque
from dataclasses import dataclass

from app.config.constant import Constant
from app.service.terminal.errors import TerminalSessionResyncRequiredError

PreviewEvent = dict[str, object]


@dataclass(frozen=True)
class _Delivery:
    event: PreviewEvent
    size: int


class TerminalPreviewSubscription:
    """先 replay 后 live 的有界订阅；溢出只通知重连，不影响其他读取者。"""

    def __init__(self) -> None:
        self._queue: queue.Queue[_Delivery] = queue.Queue(
            maxsize=Constant.Terminal.MAX_PREVIEW_QUEUE_FRAMES
        )
        self._queue_bytes = 0
        self._lock = threading.Lock()
        self._replaying = True
        self._pending: list[_Delivery] = []
        self._pending_bytes = 0
        self._closed = False
        self._overflowed = False

    @property
    def overflowed(self) -> bool:
        with self._lock:
            return self._overflowed

    def publish(self, event: PreviewEvent, size: int = 128) -> None:
        """输出按已知原始字节数计费，元数据按固定开销计费；不重新解析事件。"""
        delivery = _Delivery(dict(event), size)
        with self._lock:
            if self._closed or self._overflowed:
                return
            if self._replaying:
                if (
                    len(self._pending) >= Constant.Terminal.MAX_PREVIEW_QUEUE_FRAMES
                    or self._pending_bytes + size > Constant.Terminal.MAX_PREVIEW_QUEUE_BYTES
                ):
                    self._mark_overflow_locked()
                    return
                self._pending.append(delivery)
                self._pending_bytes += size
            elif self._queue_bytes + size > Constant.Terminal.MAX_PREVIEW_QUEUE_BYTES:
                self._mark_overflow_locked()
            else:
                self._enqueue_locked(delivery)

    def _enqueue_locked(self, delivery: _Delivery) -> bool:
        try:
            self._queue.put_nowait(delivery)
            self._queue_bytes += delivery.size
            return True
        except queue.Full:
            self._mark_overflow_locked()
            return False

    def activate(self) -> None:
        """API 发完 replay 后调用；与 publish 共用锁，确保积压事件先于新事件。"""
        with self._lock:
            if self._closed or self._overflowed or not self._replaying:
                return
            self._replaying = False
            for delivery in self._pending:
                if not self._enqueue_locked(delivery):
                    break
            self._pending.clear()
            self._pending_bytes = 0

    def get(self, timeout: float) -> PreviewEvent | None:
        if self._closed:
            return None
        try:
            delivery = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        with self._lock:
            self._queue_bytes = max(0, self._queue_bytes - delivery.size)
        return delivery.event

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._clear_locked()

    def _clear_locked(self) -> None:
        self._pending.clear()
        self._pending_bytes = 0
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._queue_bytes = 0

    def _mark_overflow_locked(self) -> None:
        self._overflowed = True
        self._clear_locked()
        # 上面已排空，且所有生产者持有同一把锁，因此这一帧不会遇到 queue.Full。
        self._queue.put_nowait(_Delivery({"type": "subscriber_overflow"}, 128))
        self._queue_bytes = 128


@dataclass(frozen=True)
class TerminalPreviewAttachment:
    attached: PreviewEvent
    replay: tuple[PreviewEvent, ...]
    terminal_event: PreviewEvent | None
    subscription: TerminalPreviewSubscription


@dataclass(frozen=True)
class _OutputFrame:
    seq: int
    encoded: str
    size: int


class PreviewBuffer:
    """原始输出窗口与订阅集合；所有方法由所属会话锁保护。"""

    def __init__(self, session_id: str, generation: str, limit: int) -> None:
        self._identity = {
            "protocol": "terminal-preview-v1",
            "session_id": session_id,
            "generation": generation,
        }
        self._limit = limit
        self._frames: deque[_OutputFrame] = deque()
        self._bytes = 0
        self._next_seq = 1
        self._subscribers: set[TerminalPreviewSubscription] = set()

    @property
    def first_available_seq(self) -> int:
        return self._frames[0].seq if self._frames else self._next_seq

    def snapshot(self) -> PreviewEvent:
        return {"first_available_seq": self.first_available_seq, "next_seq": self._next_seq}

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

    def attach(
        self,
        after_seq: int | None,
        *,
        task_id: int,
        status: str,
        geometry: dict[str, int],
        exit_code: int | None,
        end_reason: str | None,
    ) -> TerminalPreviewAttachment:
        """after_seq 类型由 API 校验；这里只判断本窗口是否仍能连续回放。"""
        if after_seq is not None and after_seq < self.first_available_seq - 1:
            raise TerminalSessionResyncRequiredError(
                after_seq, self.first_available_seq, self._next_seq
            )
        subscription = TerminalPreviewSubscription()
        replay = tuple(
            self._output_event(frame)
            for frame in self._frames
            if after_seq is None or frame.seq > after_seq
        )
        attached = {
            **self._identity,
            **self.snapshot(),
            "type": "attached",
            **geometry,
            "task_id": task_id,
            "status": status,
        }
        ended = status not in {"starting", "running"}
        if not ended:
            self._subscribers.add(subscription)
        return TerminalPreviewAttachment(
            attached,
            replay,
            self._exit_event(status, exit_code, end_reason) if ended else None,
            subscription,
        )

    def detach(self, subscription: TerminalPreviewSubscription) -> None:
        self._subscribers.discard(subscription)

    def finish(self, *, status: str, exit_code: int | None, end_reason: str | None) -> None:
        event = self._exit_event(status, exit_code, end_reason)
        for subscriber in self._subscribers:
            subscriber.publish(event)
        self._subscribers.clear()

    def _output_event(self, frame: _OutputFrame) -> PreviewEvent:
        return {**self._identity, "type": "output", "seq": frame.seq, "data_base64": frame.encoded}

    def _exit_event(
        self, status: str, exit_code: int | None, end_reason: str | None
    ) -> PreviewEvent:
        return {
            **self._identity,
            "type": "exit",
            "status": status,
            "exit_code": exit_code,
            "end_reason": end_reason,
        }
