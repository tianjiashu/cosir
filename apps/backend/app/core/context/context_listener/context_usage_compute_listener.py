"""上下文占用订阅者：``RuntimeContextManager`` 消息变更后发出 ``CONTEXT_USAGE`` 事件并回写 task。

实现 :class:`~app.core.context.context_listener.context_listener.ContextListener` 协议，
供 ``RuntimeContextManager`` 在 ``messages`` 变更时通知。仅在 ``add_message`` /
``load_history`` / ``context_compressed`` 三类事件时计算占用值：普通变更累加增量，
压缩事件以压缩后总量为准；随后经 ``write_event`` 发出 ``CONTEXT_USAGE``，并经注入的
``update_context_usage`` 回调回写 task。只负责占用统计，不负责消息读写与持久化实现。
"""

from __future__ import annotations

from collections.abc import Callable

from app.config.logging.logger import log
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
        """消息变更后计算上下文占用并写回 ``result``、发出 ``CONTEXT_USAGE`` 事件。

        仅处理 ``add_message`` / ``load_history`` / ``context_compressed`` 三类事件；
        普通变更的占用值为已有占用加上本批消息占用，压缩事件以压缩后总量为准。
        写回 ``result.usage`` 后发出事件并调用 ``update_context_usage`` 回写 task。

        参数:
            event: 监听事件（含事件类型、变更消息、已有占用与窗口上限）。
            result: 累计结果，``usage`` 被覆盖为本次计算值。

        返回:
            无。
        """
        if event.type not in [
            ContextEventType.ADD_MESSAGE,
            ContextEventType.LOAD_HISTORY,
            ContextEventType.CONTEXT_COMPRESSED,
        ]:
            return
        usage = self._compute(event.messages)
        # 压缩事件占用以压缩后总量为准，其余变更累加本批消息占用。
        if event.type == ContextEventType.CONTEXT_COMPRESSED:
            result.usage = usage
        else:
            result.usage = event.usage + usage
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
        self._update_context_usage(self.task_id, result.usage)
        if write_error is not None:
            raise write_error

    def _compute(self, messages: list[RuntimeMessage]) -> int:
        """对消息列表逐条估算并求和 token 占用。

        参数:
            messages: 本次变更涉及的运行时消息。

        返回:
            消息列表的 token 占用之和。

        异常:
            无。

        副作用:
            无。
        """
        return sum(self._message_tokens(m) for m in messages)

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
