"""``execute_terminal`` 卡片的实时输出通道：读取线程旁路出的文本批量投递到事件循环。

与 ``command_output`` 分工：收集点负责解码、整理与封顶（最终结果），本模块只负责把读取线程
旁路出的文本合并、分帧，再以 ``ToolCallRuntimeUpdateEvent`` 投递到 Run 的事件循环（只服务
运行中与后台运行中卡片的实时视图）。投递失败只记日志，不改变工具执行结果。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator

from app.config.logging.logger import log
from app.core.events import (
    TerminalCommandStartedData,
    TerminalOutputDeltaData,
    ToolCallRuntimeUpdateEvent,
)
from app.core.events.event_sink import EventSink
from app.core.events.tool_runtime_event import ToolCallRuntimeUpdateData
from app.core.tool.schemas.tool_execution_context import ToolExecutionContext

# 合并窗口：读取线程按 4KB 块回调，逐块发事件会让投影与 SSE 帧数随输出线性膨胀。
_BATCH_INTERVAL_SECONDS = 0.075
# 单个终端输出事件的文本上限；超长输出切成多帧。
_EVENT_TEXT_LIMIT = 4096


class LiveOutputChannel:
    """把读取线程产生的文本合并后，按序投递到事件循环。

    ``emit`` 由读取线程调用、``finish`` 由工具线程调用，缓冲与发布在事件循环上进行；
    ``finish`` 由作业在命令结束时调用（通道随命令而不是随工具调用结束，转到后台后照常推送），只关闭
    通道并排空已接收文本、**不阻塞**，之后到达的 ``emit`` 静默丢弃。不等待发布的理由：命令结束后
    part 随结论离开活动状态，投影会丢弃迟到帧，等待对前端没有可见收益，而事件循环已停止（未 close）
    时排队的回调永不执行，等待会把作业线程永久挂死。``emit`` 不抛异常，
    采集器据此不再包裹 sink 调用。
    """

    def __init__(
        self,
        execution_context: ToolExecutionContext,
        loop: asyncio.AbstractEventLoop,
        sink: EventSink,
    ) -> None:
        self._context = execution_context
        self._loop = loop
        self._sink = sink
        self._lock = threading.Lock()
        self._pending: list[str] = []
        self._scheduled = False
        self._draining = False
        self._timer: asyncio.TimerHandle | None = None
        self._closed = False
        self._seq = 0

    @classmethod
    def open(cls, execution_context: ToolExecutionContext) -> LiveOutputChannel | None:
        """无 Run 的直接调用没有事件循环或事件回调，此时不建实时通道。

        工具在 ``asyncio.to_thread`` 的工作线程里调用本方法，不能用 ``get_running_loop``，
        事件循环只能从执行上下文取。
        """
        loop = execution_context.event_loop
        sink = execution_context.event_sink
        if loop is None or sink is None:
            return None
        return cls(execution_context, loop, sink)

    def start(self, display_data: dict[str, object]) -> None:
        """发布卡片的初始展示数据（命令与工作目录）；须在命令拉起、``emit`` 之前调用。

        与输出块走同一个 ``call_soon_threadsafe`` 队列：事件循环按先进先出执行回调，输出块的发布
        至少晚一个合并窗口，所以初始展示必然先到，投影不需要「第一块输出顺便建展示」的特例。
        """
        data = TerminalCommandStartedData(
            kind="terminal_command_started", display_data=display_data
        )
        self._wake_loop(lambda: self._publish(data, seq=0))

    def emit(self, text: str) -> None:
        """接收读取线程的原始文本片段；通道关闭后静默丢弃。"""
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
        """关闭通道，并把已接收文本交给事件循环排空；不阻塞调用方。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            has_pending = bool(self._pending or self._scheduled or self._draining)
        if has_pending:
            self._wake_loop(self._drain)

    def _log_data(self, **extra: object) -> dict[str, object]:
        return {
            "task_id": self._context.task_id,
            "run_id": self._context.run_id,
            "tool_call_id": self._context.tool_call_id,
            **extra,
        }

    def _wake_loop(self, callback: Callable[[], None]) -> None:
        """线程安全地调度事件循环回调；事件循环已关闭时丢弃剩余输出。"""
        try:
            self._loop.call_soon_threadsafe(callback)
        except RuntimeError:
            with self._lock:
                dropped_chars = sum(map(len, self._pending))
                self._closed = True
                self._pending.clear()
                self._scheduled = False
            log.warning(
                "terminal_output_loop_unavailable",
                extra={
                    "msg": "事件循环已关闭，无法投递剩余终端输出",
                    "data": self._log_data(dropped_chars=dropped_chars),
                },
            )

    def _schedule_drain(self) -> None:
        """在事件循环上应用合并窗口；通道已关闭时立即发布。"""
        with self._lock:
            if self._timer is not None:
                return
            drain_now = self._closed
            if not drain_now:
                self._timer = self._loop.call_later(_BATCH_INTERVAL_SECONDS, self._drain)
        if drain_now:
            self._drain()

    def _drain(self) -> None:
        """发布缓冲文本；期间新到的文本再排一次 drain。"""
        with self._lock:
            timer = self._timer
            self._timer = None
            self._scheduled = False
            self._draining = True
            pending = self._pending
            self._pending = []
        if timer is not None:
            timer.cancel()

        for text in _iter_chunks(pending):
            self._publish(
                TerminalOutputDeltaData(kind="terminal_output_delta", text=text), seq=self._seq
            )
            self._seq += 1

        should_reschedule = False
        with self._lock:
            self._draining = False
            if self._pending and not self._scheduled:
                self._scheduled = True
                should_reschedule = True
        if should_reschedule:
            self._wake_loop(self._schedule_drain)

    def _publish(self, data: ToolCallRuntimeUpdateData, *, seq: int) -> None:
        """在事件循环上交给 sink；失败只记日志，最终工具结果仍会带权威展示数据到达。"""
        try:
            self._sink(
                ToolCallRuntimeUpdateEvent(
                    task_id=self._context.task_id,
                    run_id=self._context.run_id,
                    tool_call_id=self._context.tool_call_id,
                    seq=seq,
                    data=data,
                )
            )
        except Exception:
            log.exception(
                "terminal_output_publish_failed",
                extra={
                    "msg": "终端运行期更新发布失败，继续等待最终工具结果",
                    "data": self._log_data(seq=seq, update=data.kind),
                },
            )


def _iter_chunks(texts: list[str]) -> Iterator[str]:
    """按事件文本上限拆分，不丢弃或改写内容。"""
    parts: list[str] = []
    part_chars = 0
    for text in texts:
        offset = 0
        while offset < len(text):
            take = min(_EVENT_TEXT_LIMIT - part_chars, len(text) - offset)
            parts.append(text[offset : offset + take])
            part_chars += take
            offset += take
            if part_chars == _EVENT_TEXT_LIMIT:
                yield "".join(parts)
                parts.clear()
                part_chars = 0
    if parts:
        yield "".join(parts)


__all__ = ["LiveOutputChannel"]
