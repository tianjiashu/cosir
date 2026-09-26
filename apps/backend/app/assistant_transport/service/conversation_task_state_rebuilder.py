"""仅依据规范会话事实（Task、Run、context）以纯函数方式重建 Assistant Transport 状态。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import groupby
from operator import attrgetter
from typing import Any, cast

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages.tool import ToolCall

from app.assistant_transport.event import build_user_input_parts
from app.assistant_transport.state.conversation_run_snapshot import ConversationRunSnapshot
from app.assistant_transport.state.conversation_state_error import ConversationStateError
from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_part import (
    ConversationStatePart,
    ConversationStateTextPart,
    ConversationStateToolCallPart,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
)
from app.config.configuration import get_tool_registry
from app.models.conversation_run_extra import ConversationRunExtra
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
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
            ``{"code", "message"}`` 形状的 Transport 错误；Run 未失败或没有错误契约时返回
            ``None``。

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
        child_agent_roles: Mapping[tuple[int, str], str] | None = None,
    ) -> dict[str, ConversationStateToolCallPart]:
        """把单个 Run 的 context 行配对为 ``toolCallId → tool-call part`` 映射。

        配对规则：AI 消息中的每个工具调用先以 ``cancelled`` 初始状态登记，随后由同一
        ``tool_call_id`` 的 ToolMessage 行补全真实生命周期状态、``display_data`` 与错误标记。
        动态展示数据只从 ToolMessage 的 ``transport_metadata.display_data`` 读取；冷重建不
        通过工具名、委派记录或子任务记录推断展示数据。

        参数:
            rows: 单个 Run 的 context 行，调用方保证已按 ``sequence`` 排序。
            child_agent_roles: 由调用方按 child task 的 workspace 解析出的旧记录 role，键为
                ``(child_task_id, child_agent_id)``；已持久化 role 优先于此回填值。

        返回:
            以工具调用 id 为键的 tool-call part 字典；``status`` 恒为字符串，未收到结果的
            调用保持 ``cancelled``。

        异常:
            RuntimeError: ToolMessage 行找不到同一 Run 内对应的 AI 工具调用（上下文被破坏）。

        副作用:
            无（纯函数，不访问数据库、注册表之外的状态或前端运行时）。
        """
        tool_parts: dict[str, ConversationStateToolCallPart] = {}
        for row in rows:
            message: BaseMessage = row.message
            if isinstance(message, AIMessage) and cast(AIMessage, message).tool_calls:
                ai_message = cast(AIMessage, message)
                calls: list[ToolCall] = ai_message.tool_calls
                for call in calls:
                    tool_part = ConversationStateToolCallPart(
                        type="tool-call",
                        toolCallId=call.get("id"),
                        toolName=call.get("name"),
                        args=call.get("args"),
                        presentation=ConversationTaskStateRebuilder.get_tool_display(
                            call.get("name")
                        ),
                        status="cancelled",
                    )
                    tool_parts[call.get("id")] = tool_part
            if isinstance(message, ToolMessage):
                tool_message = cast(ToolMessage, message)
                tool_part: ConversationStateToolCallPart = tool_parts.get(tool_message.tool_call_id)
                if tool_part is None:
                    raise RuntimeError("未闭合tool")
                metadata_status = row.transport_metadata.get("status")
                if metadata_status is not None:
                    tool_part["status"] = metadata_status
                display_data = row.transport_metadata.get("display_data")
                if display_data is not None:
                    tool_part["display_data"] = display_data
                elif "display_data" not in tool_part:
                    tool_part["display_data"] = None
                if (
                    isinstance(display_data, dict)
                    and display_data.get("kind") == "delegation-result"
                ):
                    child_task_id = display_data.get("child_task_id")
                    if isinstance(child_task_id, int) and child_task_id > 0:
                        tool_part["child_task_id"] = child_task_id
                    child_run_id = display_data.get("child_run_id")
                    if (
                        isinstance(child_run_id, int)
                        and not isinstance(child_run_id, bool)
                        and child_run_id > 0
                    ):
                        tool_part["child_run_id"] = child_run_id
                    child_agent_id = display_data.get("child_agent_id")
                    if isinstance(child_agent_id, str) and child_agent_id:
                        persisted_role = display_data.get("role")
                        if isinstance(persisted_role, str) and persisted_role.strip():
                            tool_part["agent_role"] = persisted_role
                        else:
                            child_task_id = display_data.get("child_task_id")
                            if (
                                isinstance(child_task_id, int)
                                and not isinstance(child_task_id, bool)
                                and child_agent_roles is not None
                            ):
                                persisted_role = child_agent_roles.get(
                                    (child_task_id, child_agent_id)
                                )
                        if isinstance(persisted_role, str) and persisted_role.strip():
                            display_data = dict(display_data)
                            display_data["role"] = persisted_role
                            tool_part["display_data"] = display_data
                            tool_part["agent_role"] = persisted_role
                if tool_part["status"] == "failed":
                    tool_part["isError"] = True
                    tool_part["error"] = row.transport_metadata.get("error")
                else:
                    tool_part["isError"] = False
                    tool_part["error"] = None

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
    def build_user_message(run: ConversationRunRecord) -> ConversationStateMessage:
        """从 Run 输入事实构造可冷重建的 user 消息。

        正常执行会先写入 Human context；若进程恰好在该写入前崩溃，Run.extra 仍是已提交
        的普通附件事实，因此 snapshot 不能因为缺少 context 行而丢失用户消息或附件。
        """
        extra: ConversationRunExtra | None = getattr(run, "extra", None)
        text = extra.display_text if extra is not None else run.input_text
        user_parts: list[ConversationStatePart] = cast(
            list[ConversationStatePart],
            build_user_input_parts(
                text,
                getattr(run, "image_paths", None) or [],
                cast(
                    Sequence[Mapping[str, str]],
                    extra.attachments if extra is not None else [],
                ),
            ),
        )
        return ConversationStateMessage(
            id=f"user-{run.id}",
            role="user",
            parts=user_parts,
        )

    @staticmethod
    def rebuild(
        task: TaskRecord,
        runs: Sequence[ConversationRunRecord],
        context_rows: Sequence[ConversationTaskContextRecord],
        child_agent_roles: Mapping[tuple[int, str], str] | None = None,
    ) -> ConversationStateSnapshot:
        """从三类规范记录装配出通过校验的 Task 级 Transport 快照。

        参数:
            task: Task 级当前 Run 与上下文用量事实。
            runs: 属于 ``task`` 的全部 Run 记录。
            context_rows: 该 Task 已持久化的 LangChain 消息与 Transport 元数据。
            delegations: 为保持调用方接口稳定而保留；委派展示数据来自 context metadata。
            child_agent_roles: 调用方按 child task workspace 解析出的旧 role 回填映射。

        返回:
            新的 ``ConversationStateSnapshot``。Run 的排序为 ``created_at`` 再按 id；同一 Run 的
            assistant 行会合并为一条消息，所有消息 id 均取自 context 行 id。流式 assistant 草稿
            保留在 assistant 消息中，其文本 part 仅在所属 Run 仍活跃时为 ``running``。

        异常:
            RuntimeError: 某条工具结果行在同一 Run 内找不到对应的 AI 工具调用。

        副作用:
            无。本方法不访问 Session、快照存储、checkpoint、工具、前端运行时状态或进程内
            Transport registry。
        """

        run_records = list(runs)
        run_groups = {
            run_id: list(rows)
            for run_id, rows in groupby(
                sorted(context_rows, key=attrgetter("run_id")),
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
                child_agent_roles=child_agent_roles,
            )

            snapshot_messages: list[ConversationStateMessage] = []
            assistant_message = ConversationStateMessage(
                id=f"assistant-{run.id}", role="assistant", parts=[]
            )
            has_user_message = False
            for row in rows:
                message = row.message
                if isinstance(message, HumanMessage):
                    has_user_message = True
                    snapshot_messages.append(ConversationTaskStateRebuilder.build_user_message(run))
                elif isinstance(message, AIMessage):
                    ai_message: AIMessage = cast(AIMessage, message)
                    text_status = (
                        "running"
                        if row.is_streaming and run.status in {"pending", "running"}
                        else "completed"
                    )
                    if ai_message.additional_kwargs.get("reasoning_content") is not None:
                        assistant_message["parts"].append(
                            ConversationStateTextPart(
                                type="reasoning",
                                text=content_to_text(
                                    ai_message.additional_kwargs["reasoning_content"]
                                ),
                                status=text_status,
                            )
                        )
                    if ai_message.content is not None:
                        assistant_message["parts"].append(
                            ConversationStateTextPart(
                                type="text",
                                text=content_to_text(ai_message.content),
                                status=text_status,
                            )
                        )
                    if ai_message.tool_calls is not None and len(ai_message.tool_calls) > 0:
                        calls: list[ToolCall] = ai_message.tool_calls
                        for call in calls:
                            assistant_message["parts"].append(tool_parts_dict[call.get("id")])
            run_extra: ConversationRunExtra | None = getattr(run, "extra", None)
            if not has_user_message and (
                getattr(run, "input_text", "").strip()
                or getattr(run, "image_paths", None)
                or (run_extra.attachments if run_extra is not None else [])
            ):
                snapshot_messages.append(ConversationTaskStateRebuilder.build_user_message(run))
            snapshot_messages.append(assistant_message)
            snapshot_runs.append(
                ConversationRunSnapshot(
                    runId=run.id,
                    status=run.status,
                    endReason=run.end_reason,
                    messages=snapshot_messages,
                    usage=run.usage,
                    error=ConversationTaskStateRebuilder.build_run_error(run),
                )
            )
        used = task.context_usage_used
        total = task.context_window_total
        ratio = None if used is None or total is None or total == 0 else used / total
        return ConversationStateSnapshot(
            runs=snapshot_runs,
            current_run_id=task.current_run_id,
            context_usage_used=used,
            context_window_total=total,
            error=None,
            context_usage_ratio=ratio,
            approvals={},
        )


__all__ = [
    "ConversationTaskStateRebuilder",
]
