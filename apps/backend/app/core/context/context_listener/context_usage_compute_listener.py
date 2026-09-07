"""上下文占用订阅者：计算上下文占用并回写 task 事实。

实现 :class:`~app.core.context.context_listener.context_listener.ContextListener` 协议，
供 ``RuntimeContextManager`` 在有效上下文条目变更时通知。仅在 ``add_message`` /
``load_history`` / ``context_compressed`` 三类事件时计算占用值：所有事件均以有效上下文
完整快照重算；随后经 ``write_event`` 发出 ``CONTEXT_USAGE``，并经注入的
``update_context_usage`` 回调回写 task。只负责占用统计——只消费条目的消息部分，不关心
turn 归属，也不负责消息读写与持久化实现。
"""

from __future__ import annotations

from collections.abc import Callable

from langchain_core.messages import BaseMessage

from app.core.context.context_entry import ContextEntry
from app.core.context.context_listener.context_listener import ContextListener
from app.core.context.context_listener.listener_event import ContextEventType, ListenerEvent
from app.core.context.context_listener.listener_result import ListenerResult
from app.core.workflows.event import ContextUsageUpdatedEvent, ConversationEvent
from app.service.depends import get_task_service, get_conversation_event_projector
from app.utils.message_content import content_to_text


class ContextUsageComputeListener(ContextListener):
    main_agent_only = True
    order = 1

    def __init__(
        self,
        task_id: int,
        run_id: int | None = None,
    ) -> None:
        """构造订阅者，注入事件发布器及 task/run 标识。

        参数:
            task_id: 所属 task，用于回写 context usage。
            run_id: 当前 Conversation Run 标识；初始化历史阶段可为空。
            publish_event: 可选的中性事件发布器，由 workflow 注入，不能依赖 LangGraph
                隐式 runnable context。

        返回:
            无。

        异常:
            无。

        副作用:
            保存事件写入回调、task 占用回写回调与 task 标识。
        """
        self.task_id = task_id
        self.run_id = run_id
        self.event_projector = get_conversation_event_projector()

    def _emit_context_usage(self, task_id: int, used: int, total: int) -> None:
        """通过显式事件发布器发布上下文占用事件。"""


        ratio = used / total if total > 0 else 0.0
        self.event_projector.process(
            ContextUsageUpdatedEvent(
                task_id=task_id,
                run_id=self.run_id,
                ratio=ratio,
                used_tokens=used,
            )
        )

    def listen(self, event: ListenerEvent, result: ListenerResult) -> None:
        """条目变更后计算上下文占用并写回 ``result`` 与 task 事实。

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
            经显式事件发布器发出 ``CONTEXT_USAGE`` 事件，并回写 task 上下文占用。
        """
        if event.type not in [
            ContextEventType.ADD_MESSAGE,
            ContextEventType.LOAD_HISTORY,
            ContextEventType.CONTEXT_COMPRESSED,
        ]:
            return
        # ``event.entries`` 是变化后的有效模型上下文快照，统一重算避免重复累加。
        result.usage = self._compute(event.entries)
        self._emit_context_usage(self.task_id, result.usage, event.total_tokens)
        get_task_service().update_context_usage(self.task_id, result.usage)

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
    def _message_tokens(message: BaseMessage) -> int:
        """估算单条消息的 token 占用。

        基于消息正文和 assistant 的 ``tool_calls`` 序列化内容估算。

        参数:
            message: 待估算的运行时消息。

        返回:
            单条消息的 token 占用。

        异常:
            无。

        副作用:
            无。
        """
        tool_calls = getattr(message, "tool_calls", None)
        text = content_to_text(message.content)
        if tool_calls:
            text += str(tool_calls)
        return max(1, len(text) // 4) if text else 0
