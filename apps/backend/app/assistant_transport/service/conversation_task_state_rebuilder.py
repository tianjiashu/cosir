"""仅依据规范会话事实（Task、Run、context）以纯函数方式重建 Assistant Transport 状态。"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import groupby
from operator import attrgetter
from typing import cast

from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
    ChatCompletionUserMessageParam,
)

from app.assistant_transport.event import build_user_input_parts
from app.assistant_transport.projection.tool_result import project_tool_result
from app.assistant_transport.state.conversation_run_snapshot import ConversationRunSnapshot
from app.assistant_transport.state.conversation_state_error import ConversationStateError
from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_part import (
    ConversationStateTextPart,
    ConversationStateToolCallPart,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
)
from app.config.configuration import get_tool_registry
from app.core.llm_provider.message_codec import display_arguments
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.message_metadata import MessageMetadata, ToolMessageMetadata, UserMessageMetadata
from app.models.task_record import TaskRecord
from app.utils.message_content import content_to_text


class ConversationTaskStateRebuilder:
    """仅依据 Task、Run 与 context 记录，重建 Task 级 Transport 快照。"""

    @staticmethod
    def build_run_error(run: ConversationRunRecord) -> ConversationStateError | None:
        """把持久化的 Run 错误契约投影为 Transport 错误契约。

        参数:
            run: 已落库的 Run 记录；其 ``error`` 来自 ``error_json``，由
                ``ConversationRunStateService.terminal_error`` 写入。

        返回:
            ``{"code", "message"}`` 形状的 Transport 错误；``message`` 保留持久化错误契约
            原值（模型 HTTP 失败时可能是响应 message）；Run 未失败或没有错误契约时返回 ``None``。

        异常:
            ValueError: 持久化错误契约不是映射，或不是恰好 ``{code, message}`` 两个**非空白**
                字符串——受控字段被污染时显式失败，而不是把非法数据投影成面向用户的文案
                （空白文案会让前端渲染出没有内容的错误气泡）。

        副作用:
            无（纯函数，不访问数据库或进程内状态）。
        """

        error = run.error
        if error is None:
            return None
        if not isinstance(error, dict):
            raise ValueError(f"run {run.id} carries a non-mapping error contract")
        code = error.get("code")
        message = error.get("message")
        if (
            set(error) != {"code", "message"}
            or not isinstance(code, str)
            or not code.strip()
            or not isinstance(message, str)
            or not message.strip()
        ):
            raise ValueError(f"run {run.id} carries a malformed error contract")
        return ConversationStateError(code=code, message=message)

    @staticmethod
    def build_pair_tool_part(
        rows: list[ConversationTaskContextRecord],
    ) -> dict[str, ConversationStateToolCallPart]:
        """把单个 Run 的 context 行配对为 ``toolCallId → tool-call part`` 映射。

        配对规则：AI 消息中的每个工具调用先以 ``cancelled`` 初始状态登记，随后由同一
        ``tool_call_id`` 的 tool 消息 行补全真实生命周期状态、``display_data`` 与错误标记。
        动态展示数据只从 tool 消息 的 ``metadata.display_data`` 读取；冷重建不
        通过工具名、委派记录或子任务记录推断展示数据。

        **隐藏调用不建 part**：tool 消息 行带 ``metadata.hidden`` 的调用（本轮未
        放行 / 工具名未注册 — 实时链路从未为它们发过 ``ToolCallCreatedEvent``）被整条跳过，
        否则刷新页面会凭空多出实时快照里不存在的工具气泡。

        参数:
            rows: 单个 Run 的 context 行，调用方保证已按 ``sequence`` 排序。

        返回:
            以工具调用 id 为键的 tool-call part 字典；``status`` 恒为字符串，未收到结果的
            调用保持 ``cancelled``。

        异常:
            RuntimeError: tool 消息 行找不到同一 Run 内对应的 AI 工具调用（上下文被破坏）。

        副作用:
            无（纯函数，不访问数据库、注册表之外的状态或前端运行时）。
        """
        # 先扫一遍结果行收集隐藏调用：AI 消息的 tool_calls 里可能包含它们，但实时链路从未为
        # 这些调用建过 part，冷重建必须与实时口径一致地跳过。
        hidden_call_ids = {
            row.message.get("tool_call_id")
            for row in rows
            if row.message["role"] == "tool"
            and isinstance(row.metadata, ToolMessageMetadata)
            and row.metadata.hidden
        }
        tool_parts: dict[str, ConversationStateToolCallPart] = {}
        for row in rows:
            message: ChatCompletionMessageParam = row.message
            if message["role"] == "assistant" and message.get("tool_calls"):
                ai_message = message
                calls = cast(
                    list[ChatCompletionMessageFunctionToolCallParam],
                    ai_message.get("tool_calls") or [],
                )
                for call in calls:
                    # 中断输出可能只有部分调用身份；尚未建立的工具预览不重建为 part。
                    if not call["id"] or not call["function"]["name"]:
                        continue
                    if call["id"] in hidden_call_ids:
                        continue
                    tool_part = ConversationStateToolCallPart(
                        type="tool-call",
                        toolCallId=call["id"],
                        toolName=call["function"]["name"],
                        args=display_arguments(call["function"]["arguments"]),
                        presentation=ConversationTaskStateRebuilder.get_tool_display(
                            call["function"]["name"]
                        ),
                        status="cancelled",
                    )
                    tool_parts[call["id"]] = tool_part
            if message["role"] == "tool" and isinstance(row.metadata, ToolMessageMetadata):
                tool_message = message
                if tool_message["tool_call_id"] in hidden_call_ids:
                    continue
                paired_part = tool_parts.get(tool_message["tool_call_id"])
                if paired_part is None:
                    raise RuntimeError("未闭合tool")
                tool_part = paired_part
                tool_part.update(project_tool_result(row.metadata))

        return tool_parts

    @staticmethod
    def get_tool_display(tool_name: str) -> dict[str, object]:
        """返回 ``tool_name`` 的静态展示声明（已序列化为 dict）。

        参数:
            tool_name: AI 工具调用携带的已注册工具名。

        返回:
            ``ToolDisplayHints`` 序列化后的 dict。当 ``tool_name`` 为空、工具未注册或未声明
            ``display`` 时返回**空 dict**（绝不返回 ``None``），以保证重建出的快照 part 的
            ``presentation`` 字段始终是合法对象、能通过 ``validate_snapshot``；这与流式投影
            路径在同一「缺少展示声明」场景下恒发 ``{}`` 的行为保持一致。
        """
        if not tool_name:
            return {}
        tool_registry = get_tool_registry()
        tool_definition = tool_registry.get_tool_definition(tool_name)
        if not tool_definition or not tool_definition.display:
            return {}
        return tool_definition.display.to_dict()

    @staticmethod
    def build_user_message(
        message_id: str, message: ChatCompletionUserMessageParam, metadata: MessageMetadata
    ) -> ConversationStateMessage:
        """用户展示绑定来自记录 metadata，消息身份独立于 SDK 正文与 Run 首条输入。"""
        payload = metadata.presentation if isinstance(metadata, UserMessageMetadata) else None
        if payload is None:
            return ConversationStateMessage(
                id=message_id,
                role="user",
                parts=[
                    {
                        "type": "text",
                        "text": content_to_text(message.get("content")),
                        "status": "completed",
                    }
                ],
            )
        return ConversationStateMessage(
            id=message_id,
            role="user",
            parts=[
                *build_user_input_parts(
                    payload.display_text,
                    payload.image_paths,
                    payload.attachments,
                )
            ],
        )

    @staticmethod
    def rebuild(
        task: TaskRecord,
        runs: Sequence[ConversationRunRecord],
        context_rows: Sequence[ConversationTaskContextRecord],
    ) -> ConversationStateSnapshot:
        """从三类规范记录装配出通过校验的 Task 级 Transport 快照。

        参数:
            task: Task 级当前 Run 与上下文用量事实。
            runs: 属于 ``task`` 的全部 Run 记录。
            context_rows: 该 Task 已持久化的 领域消息与 Transport 元数据。

        返回:
            新的 ``ConversationStateSnapshot``。Run 的排序为 ``created_at`` 再按 id；同一 Run 的
            assistant 行按用户消息分组展示。中断输出也保留展示；已落库文本均已停止生成，
            不由 Run 状态反推。

        异常:
            RuntimeError: 某条工具结果行在同一 Run 内找不到对应的 AI 工具调用。

        副作用:
            无。本方法不访问 Session、快照存储、checkpoint、工具、前端运行时状态或进程内
            Transport registry。
        """

        run_records = list(runs)
        # Task 系统提示词是模型上下文事实，不属于任何 Conversation Run，也不应出现在
        # Assistant Transport 快照中。其合法的 ``run_id=None`` 不能参与下方的整数分组排序。
        run_context_rows = [row for row in context_rows if row.run_id is not None]
        run_groups = {
            run_id: list(rows)
            for run_id, rows in groupby(
                sorted(run_context_rows, key=attrgetter("run_id")),
                key=attrgetter("run_id"),
            )
        }

        snapshot_runs: list[ConversationRunSnapshot] = []

        for run in run_records:
            # Run 没有 context 行也必须出现在快照里：`current_run_id` 必须指向快照中的某个
            # Run（``validate_snapshot`` 强校验），而新建 Run 在写入首条消息前正是这个状态。
            rows: list[ConversationTaskContextRecord] = sorted(
                run_groups.get(run.id, []), key=lambda row: row.sequence
            )

            tool_parts_dict = ConversationTaskStateRebuilder.build_pair_tool_part(
                rows,
            )

            snapshot_messages: list[ConversationStateMessage] = []
            assistant_message = ConversationStateMessage(
                id=f"assistant-{run.id}", role="assistant", parts=[]
            )
            has_user_message = False
            for row in rows:
                if not row.project_to_transport:
                    continue
                message = row.message
                if message["role"] == "user":
                    has_user_message = True
                    if assistant_message["parts"]:
                        snapshot_messages.append(assistant_message)
                    snapshot_messages.append(
                        ConversationTaskStateRebuilder.build_user_message(
                            row.message_id, message, row.metadata
                        )
                    )
                    assistant_message = ConversationStateMessage(
                        id=f"assistant-{row.message_id}", role="assistant", parts=[]
                    )
                elif message["role"] == "assistant":
                    ai_message: ChatCompletionAssistantMessageParam = message
                    if ai_message.get("reasoning_content") is not None:
                        assistant_message["parts"].append(
                            ConversationStateTextPart(
                                type="reasoning",
                                text=content_to_text(ai_message.get("reasoning_content")),
                                status="completed",
                            )
                        )
                    if ai_message.get("content") is not None:
                        assistant_message["parts"].append(
                            ConversationStateTextPart(
                                type="text",
                                text=content_to_text(ai_message.get("content")),
                                status="completed",
                            )
                        )
                    if ai_message.get("tool_calls"):
                        calls = cast(
                            list[ChatCompletionMessageFunctionToolCallParam],
                            ai_message.get("tool_calls") or [],
                        )
                        for call in calls:
                            tool_part = tool_parts_dict.get(call["id"])
                            if tool_part is None:
                                # 隐藏调用（本轮未放行 / 工具名未注册）：``build_pair_tool_part``
                                # 不为它们建 part，实时链路同样没有 part，此处必须跳过而不是
                                # 抛 KeyError——展示层缺口不得让冷重建整张快照失败。
                                continue
                            assistant_message["parts"].append(tool_part)
            if has_user_message or assistant_message["parts"]:
                snapshot_messages.append(assistant_message)
            snapshot_runs.append(
                ConversationRunSnapshot(
                    runId=run.id,
                    status=run.status,
                    endReason=run.end_reason,
                    contextWindowTotal=run.context_window_total,
                    messages=snapshot_messages,
                    usage=run.usage,
                    error=ConversationTaskStateRebuilder.build_run_error(run),
                )
            )
        return ConversationStateSnapshot(
            runs=snapshot_runs,
            current_run_id=task.current_run_id,
            error=None,
            approvals={},
        )


__all__ = [
    "ConversationTaskStateRebuilder",
]
