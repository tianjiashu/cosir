"""上下文占用订阅者：计算上下文占用并回写 task 事实。

实现 :class:`~app.core.context.context_listener.context_listener.ContextListener` 协议，
供 ``RuntimeContextManager`` 在有效上下文条目变更时通知。仅在 ``add_message`` /
``load_history`` / ``context_compressed`` 三类事件时计算占用值：所有事件均以有效上下文
完整快照重算；随后通过 ``ConversationEventProjector`` 发出
``ContextUsageUpdatedEvent``，并旁路回写 task。只负责占用统计——只消费条目的消息部分，
并加上当前 Run 的模型侧工具 schema 估算；不负责消息读写与持久化实现。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.assistant_transport.event import ContextUsageUpdatedEvent
from app.config.logging.logger import log
from app.core.context.context_entry import ContextEntry
from app.core.context.context_listener.context_listener import ContextListener
from app.core.context.context_listener.listener_event import ContextEventType, ListenerEvent
from app.core.context.context_listener.listener_result import ListenerResult
from app.service.depends import get_conversation_event_projector, get_task_service
from app.utils.message_content import content_to_text
from app.utils.token_estimator import TokenEstimator


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

        返回:
            无。

        异常:
            无。

        副作用:
            保存 projector 与 task 标识。
        """
        self.task_id = task_id
        self.run_id = run_id
        self.event_projector = get_conversation_event_projector()

    def _emit_context_usage(
        self,
        task_id: int,
        used: int,
        total: int,
        reproject: bool,
    ) -> None:
        """通过显式事件发布器发布上下文占用事件。"""

        ratio = used / total if total > 0 else 0.0
        self.event_projector.process(
            ContextUsageUpdatedEvent(
                task_id=task_id,
                run_id=self.run_id,
                ratio=ratio,
                used_tokens=used,
                context_window_tokens=total,
                reproject=reproject,
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
        # ``event.entries`` 是变化后的有效模型上下文快照，统一重算避免重复累加；
        # tool schema 是本次模型请求的输入配置，不属于持久化 message entries，因此由
        # ListenerEvent 单独携带并在此处合并计算。
        result.usage = self._compute(event.entries, event.tool_schemas)
        try:
            get_task_service().update_context_usage(self.task_id, result.usage, event.total_tokens)
        except Exception:
            # Task 表是可恢复的上下文事实；数据库失败时不发布无法从持久化事实重建的事件。
            log.exception(
                "context_usage_task_update_failed",
                extra={
                    "msg": "任务上下文占用旁路回写失败",
                    "data": {"task_id": self.task_id, "used_tokens": result.usage},
                },
            )
            return
        self._emit_context_usage(
            self.task_id,
            result.usage,
            event.total_tokens,
            event.type is ContextEventType.LOAD_HISTORY,
        )

    def _compute(
        self,
        entries: list[ContextEntry],
        tool_schemas: Sequence[Mapping[str, Any]],
    ) -> int:
        """估算完整模型输入的 token 占用并求和。

        参数:
            entries: 变化后的有效模型上下文完整快照（``ContextEntry`` 列表）。
            tool_schemas: 当前 Run 实际绑定给模型的工具 schema。

        返回:
            全部消息与工具 schema 的 token 占用之和。

        异常:
            无。

        副作用:
            无。
        """
        message_tokens = sum(self._message_tokens(entry.message) for entry in entries)
        return message_tokens + self._tool_schema_tokens(tool_schemas)

    @staticmethod
    def _tool_schema_tokens(
        tool_schemas: Sequence[Mapping[str, Any]],
    ) -> int:
        """估算模型请求中工具定义的 token 占用。

        ``bind_tools(strict=True)`` 会把裸工具 schema 转成 OpenAI-compatible function
        payload，并可能补充 ``strict`` / ``additionalProperties`` 等字段。这里复用
        LangChain 的同一转换函数后再序列化，避免只统计工具名称或 Python ``repr``，使
        估算对象尽量接近实际请求。不同 provider 的协议封装和 tokenizer 仍可能有误差，
        因此本方法只用于上下文占用提示，不作为计费事实。

        参数:
            tool_schemas: workflow 生成的模型侧裸工具 schema。

        返回:
            所有工具定义 JSON payload 的启发式 token 估算总和。

        异常:
            不向上抛出 schema 转换或序列化异常；单个异常工具会记录 warning 并跳过。

        副作用:
            schema 非法时写一条结构化 warning 日志。
        """
        total = 0
        for schema in tool_schemas:
            try:
                normalized = convert_to_openai_tool(dict(schema), strict=True)
                serialized = json.dumps(
                    normalized,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                total += TokenEstimator.estimate(serialized)
            except Exception as exc:
                log.warning(
                    "context_usage_tool_schema_estimate_failed",
                    extra={
                        "msg": "工具 schema token 估算失败，已跳过该工具",
                        "data": {
                            "tool_name": schema.get("name"),
                            "error_type": type(exc).__name__,
                        },
                    },
                )
        return total

    @staticmethod
    def _message_tokens(message: BaseMessage) -> int:
        """估算单条消息的 token 占用。

        基于消息正文、assistant 的 ``tool_calls`` 以及 tool message 的
        ``tool_call_id`` 估算。``tool_call_id`` 是模型协议中用于关联工具结果的字段，
        不能因为 ``ToolMessage.content`` 为空而完全忽略。

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
        invalid_tool_calls = getattr(message, "invalid_tool_calls", None)
        tool_call_id = getattr(message, "tool_call_id", None)
        text = content_to_text(message.content)
        if tool_calls:
            text += json.dumps(
                tool_calls,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        if invalid_tool_calls:
            text += json.dumps(
                invalid_tool_calls,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        if tool_call_id:
            text += str(tool_call_id)
        return TokenEstimator.estimate(text)
