"""上下文占用订阅者：``RuntimeContextManager`` 条目变更后发出 ``CONTEXT_USAGE`` 事件并回写 task。

实现 :class:`~app.core.context.context_listener.context_listener.ContextListener` 协议，
供 ``RuntimeContextManager`` 在有效上下文条目变更时通知。仅在 ``add_message`` /
``load_history`` / ``context_compressed`` 三类事件时计算占用值：所有事件均以有效上下文
完整快照重算；随后经 ``write_event`` 发出 ``CONTEXT_USAGE``，并经注入的
``update_context_usage`` 回调回写 task。只负责占用统计——只消费条目的消息部分，不关心
turn 归属，也不负责消息读写与持久化实现。
"""

from __future__ import annotations

from collections.abc import Callable

from app.config.logging.logger import log
from app.core.context.context_entry import ContextEntry
from app.core.context.context_listener.context_listener import ContextListener
from app.core.context.context_listener.listener_event import ContextEventType, ListenerEvent
from app.core.context.context_listener.listener_result import ListenerResult
from app.models import RuntimeMessage
from app.models.enums.event_type import EventType
from app.models.payload import ContextUsagePayload, RuntimeEventPayload


class ContextUsageComputeListener(ContextListener):
    main_agent_only = True
    order = 1

    def __init__(
        self,
        write_event: Callable[[EventType, RuntimeEventPayload], None],
        update_context_usage: Callable[[int, int], None],
        task_id: int,
    ) -> None:
        """构造订阅者，注入事件写入与占用回写回调及 task 标识。

        参数:
            write_event: 发出 ``CONTEXT_USAGE`` 事件的回调。
            update_context_usage: 回写 task 上下文占用的回调，签名 ``(task_id, used_tokens)``。
            task_id: 所属 task，用于回写 context usage。

        返回:
            无。

        异常:
            无。

        副作用:
            保存事件写入回调、task 占用回写回调与 task 标识。
        """
        self.write_event = write_event
        self._update_context_usage = update_context_usage
        self.task_id = task_id

    def listen(self, event: ListenerEvent, result: ListenerResult) -> None:
        """条目变更后计算上下文占用并写回 ``result``、发出 ``CONTEXT_USAGE`` 事件。

        仅处理 ``add_message`` / ``load_history`` / ``context_compressed`` 三类事件；
        所有相关事件均基于有效模型上下文完整快照重算，避免重复累加和排除非上下文轨迹。
        快照是 :class:`ContextEntry` 列表，本订阅者只取其中的 ``message`` 估算，忽略
        turn 归属（占用统计与条目归属无关）。写回 ``result.usage`` 后发出事件并调用
        ``update_context_usage`` 回写 task。

        参数:
            event: 监听事件（含事件类型、变更条目、已有占用与窗口上限）。
            result: 累计结果，``usage`` 被覆盖为本次计算值。

        返回:
            无。

        异常:
            RuntimeError: 事件写入失败且 ``event.allow_write_event_failure`` 为假时，
                在回写 task 之后原样抛出，保证失败不被静默吞掉。

        副作用:
            经 ``write_event`` 发出 ``CONTEXT_USAGE`` 事件，并经 ``update_context_usage``
            回写 task 上下文占用。
        """
        if event.type not in [
            ContextEventType.ADD_MESSAGE,
            ContextEventType.LOAD_HISTORY,
            ContextEventType.CONTEXT_COMPRESSED,
        ]:
            return
        # ``event.entries`` 是变化后的有效模型上下文快照，统一重算避免重复累加。
        result.usage = self._compute(event.entries)
        write_error: RuntimeError | None = None
        try:
            self.write_event(
                EventType.CONTEXT_USAGE,
                ContextUsagePayload(used_tokens=result.usage, total_tokens=event.total_tokens),
            )
        except RuntimeError as exc:
            if not event.allow_write_event_failure:
                write_error = exc
            else:
                log.debug(
                    "context_usage_event_writer_unavailable",
                    extra={"task_id": self.task_id, "context_event_type": event.type.value},
                )
        # 原样传播调用方回调的失败：容错策略由注入方决定（生产默认实现
        # ``_update_task_context_usage`` 自行降级，显式注入的回调按契约传播）。
        self._update_context_usage(self.task_id, result.usage)
        if write_error is not None:
            raise write_error

    def _compute(self, entries: list[ContextEntry]) -> int:
        """对上下文条目逐条估算并求和 token 占用。

        参数:
            entries: 变化后的有效模型上下文完整快照（``ContextEntry`` 列表）。

        返回:
            全部条目消息的 token 占用之和。

        异常:
            无。

        副作用:
            无。
        """
        return sum(self._message_tokens(entry.message) for entry in entries)

    @staticmethod
    def _message_tokens(message: RuntimeMessage) -> int:
        """估算单条消息的 token 占用。

        委托 :meth:`RuntimeMessage.estimate_tokens` 统计（含 assistant 消息的
        ``tool_calls`` 序列化部分）；非 ``RuntimeMessage`` 元素返回 0，不抛。

        参数:
            message: 待估算的运行时消息。

        返回:
            单条消息的 token 占用；非 ``RuntimeMessage`` 返回 0。

        异常:
            无。

        副作用:
            无。
        """
        if not isinstance(message, RuntimeMessage):
            return 0
        return message.estimate_tokens()
