"""进程隔离工具输出的线程安全缓冲与事件循环投递。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator

from app.config.logging.logger import log
from app.core.tools.schemas.tool_output import ProcessToolOutputChannel

_BATCH_INTERVAL_SECONDS = 0.075


class BufferedProcessToolOutputChannel(ProcessToolOutputChannel):
    """把工作线程产生的文本合并后，按序投递到指定事件循环。

    本类只负责线程安全缓冲、合并、分帧及投递时序，不依赖 Assistant Transport 的事件
    类型。调用方通过 ``publish`` 注入协议适配函数。事件循环关闭或发布失败时只记录日志，
    不改变工具执行结果；``finish`` 会等待已接收文本全部发布后再返回。
    """

    def __init__(
        self,
        *,
        task_id: int,
        run_id: int,
        tool_call_id: str,
        loop: asyncio.AbstractEventLoop,
        publish: Callable[[int, str], None],
        max_chunk_chars: int,
        batch_interval_seconds: float = _BATCH_INTERVAL_SECONDS,
    ) -> None:
        """绑定单次工具调用、事件循环和协议发布函数。

        ``publish`` 接收从零开始的递增序号与原始文本帧。帧大小由协议适配器提供，
        以避免核心执行层依赖具体 wire schema。
        """
        if max_chunk_chars < 1:
            raise ValueError("max_chunk_chars must be positive")
        if batch_interval_seconds < 0:
            raise ValueError("batch_interval_seconds must not be negative")
        self._task_id = task_id
        self._run_id = run_id
        self._tool_call_id = tool_call_id
        self._loop = loop
        self._publish = publish
        self._max_chunk_chars = max_chunk_chars
        self._batch_interval_seconds = batch_interval_seconds
        self._lock = threading.Lock()
        self._pending: list[str] = []
        self._scheduled = False
        self._draining = False
        self._timer: asyncio.TimerHandle | None = None
        self._closed = False
        self._finish_waiter: threading.Event | None = None
        self._seq = 0

    def emit(self, text: str) -> None:
        """接收父进程队列读取到的原始文本片段。"""
        if not text:
            return
        should_wake = False
        with self._lock:
            if self._closed:
                return
            self._pending.append(text)
            if not self._scheduled:
                self._scheduled = True
                should_wake = True
        if should_wake:
            self._wake_loop(self._schedule_drain)

    def finish(self) -> None:
        """等待所有已接收文本帧完成发布，然后关闭通道。"""
        waiter = threading.Event()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            has_pending = bool(self._pending or self._scheduled or self._draining)
            if not has_pending:
                return
            self._finish_waiter = waiter

        self._wake_loop(self._drain)
        waiter.wait()

    def _wake_loop(self, callback: Callable[[], None]) -> None:
        """线程安全地调度事件循环回调；事件流故障不改变工具结果。"""
        try:
            self._loop.call_soon_threadsafe(callback)
        except RuntimeError:
            with self._lock:
                dropped_chars = sum(map(len, self._pending))
                self._closed = True
                self._pending.clear()
                self._scheduled = False
                waiter = self._finish_waiter
                self._finish_waiter = None
            if waiter is not None:
                waiter.set()
            log.warning(
                "process_tool_output_loop_unavailable",
                extra={
                    "msg": "事件循环已关闭，无法投递剩余工具输出",
                    "data": {
                        "task_id": self._task_id,
                        "run_id": self._run_id,
                        "tool_call_id": self._tool_call_id,
                        "dropped_chars": dropped_chars,
                    },
                },
            )

    def _schedule_drain(self) -> None:
        """在事件循环上应用短暂的合并窗口。"""
        with self._lock:
            if self._timer is not None:
                return
            if self._closed:
                drain_now = True
            else:
                drain_now = False
                self._timer = self._loop.call_later(
                    self._batch_interval_seconds, self._drain
                )
        if drain_now:
            self._drain()

    def _drain(self) -> None:
        """发布缓冲文本，并在清空后唤醒等待中的进程执行线程。"""
        with self._lock:
            timer = self._timer
            self._timer = None
            self._scheduled = False
            self._draining = True
            pending = self._pending
            self._pending = []
        if timer is not None:
            timer.cancel()

        for text in self._iter_chunks(pending):
            try:
                self._publish(self._seq, text)
            except Exception:
                log.exception(
                    "process_tool_output_publish_failed",
                    extra={
                        "msg": "工具输出发布失败，继续等待最终工具结果",
                        "data": {
                            "task_id": self._task_id,
                            "run_id": self._run_id,
                            "tool_call_id": self._tool_call_id,
                            "seq": self._seq,
                        },
                    },
                )
            finally:
                self._seq += 1

        should_reschedule = False
        with self._lock:
            self._draining = False
            if self._pending:
                if not self._scheduled:
                    self._scheduled = True
                    should_reschedule = True
                waiter = None
            else:
                self._scheduled = False
                waiter = self._finish_waiter if self._closed else None
                if waiter is not None:
                    self._finish_waiter = None
        if should_reschedule:
            self._wake_loop(self._schedule_drain)
            return
        if waiter is not None:
            waiter.set()

    def _iter_chunks(self, texts: list[str]) -> Iterator[str]:
        """按协议适配器指定的帧大小拆分文本，不丢弃或改写内容。"""
        parts: list[str] = []
        part_chars = 0
        for text in texts:
            offset = 0
            while offset < len(text):
                take = min(self._max_chunk_chars - part_chars, len(text) - offset)
                parts.append(text[offset : offset + take])
                part_chars += take
                offset += take
                if part_chars == self._max_chunk_chars:
                    yield "".join(parts)
                    parts.clear()
                    part_chars = 0
        if parts:
            yield "".join(parts)


__all__ = ["BufferedProcessToolOutputChannel"]
