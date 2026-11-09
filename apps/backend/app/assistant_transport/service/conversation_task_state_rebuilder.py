"""仅依据规范会话事实（Task、Run、context）以纯函数方式重建 Assistant Transport 状态。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import groupby
from operator import attrgetter
from typing import Literal, cast

from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
)

from app.assistant_transport.projection.final_reply import is_final_reply
from app.assistant_transport.projection.snapshot_navigation import message
from app.assistant_transport.projection.tool_presentation import tool_presentation
from app.assistant_transport.projection.tool_result import project_tool_result
from app.assistant_transport.projection.user_input_projection import (
    build_agent_message_part,
    build_turn_input,
)
from app.assistant_transport.state.conversation_state_activity import (
    ConversationStateActivity,
    run_launch_snapshot,
)
from app.assistant_transport.state.conversation_state_error import ConversationStateError
from app.assistant_transport.state.conversation_state_message import (
    ConversationStateMessage,
    ConversationStateOutcome,
)
from app.assistant_transport.state.conversation_state_part import (
    ConversationStateTextPart,
    ConversationStateToolCallPart,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
)
from app.assistant_transport.state.message_identity import reply_id, user_message_id
from app.core.tool.call_intent import display_call_arguments
from app.core.tool.presentation import ToolPresentation
from app.models.conversation_input import MessageSource, UserInputPresentation
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import (
    ConversationTaskContextRecord,
    run_last_turn_withdrawn,
)
from app.models.enums.conversation_run_status import TERMINAL_RUN_STATUSES, RunTransition
from app.models.message_metadata import (
    TURN_INPUT_KINDS,
    AssistantMessageMetadata,
    ToolMessageMetadata,
    UserMessageMetadata,
)
from app.models.task_record import TaskRecord
from app.utils.datetime_utils import to_text
from app.utils.message_content import content_to_text

# 能恢复的状态就是恢复迁移的来源状态（与实时投影同一事实）。
_RESUMABLE_RUN_STATUSES = frozenset(status.value for status in RunTransition.RESUME.sources)


class ConversationTaskStateRebuilder:
    """仅依据 Task、Run 与 context 记录，重建 Task 级 Transport 快照。"""

    @staticmethod
    def build_run_error(run: ConversationRunRecord) -> ConversationStateError | None:
        """把持久化的 Run 错误契约投影为 Transport 错误契约。

        参数:
            run: 已落库的 Run 记录；其 ``error`` 来自 ``error_json``，由
                ``run_terminal_error`` 写入。

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
        presentations: Mapping[str, ToolPresentation] | None = None,
    ) -> dict[str, ConversationStateToolCallPart]:
        """把 context 按调用 id 配对；id 在 Task 内唯一，由模型响应持久化入口保证。

        配对规则：AI 消息中的每个工具调用先以 ``cancelled`` 初始状态登记，随后由同一
        ``tool_call_id`` 的 tool 消息 行补全真实生命周期状态、``display_data`` 与错误标记。
        动态展示数据只从 tool 消息 的 ``metadata.display_data`` 读取；冷重建不
        通过工具名、委派记录或子任务记录推断展示数据。

        参数:
            rows: 单个 Run 的 context 行，调用方保证已按 ``sequence`` 排序。

        返回:
            以调用 id 为键的 tool-call part 字典；``status`` 恒为字符串，未收到结果的
            调用保持 ``cancelled``。

        异常:
            RuntimeError: tool 消息 行找不到同一 Run 内对应的 AI 工具调用（上下文被破坏）。

        副作用:
            无（纯函数，不访问数据库、注册表之外的状态或前端运行时）。
        """
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
                    intent, arguments = display_call_arguments(call["function"]["arguments"])
                    tool_part = ConversationStateToolCallPart(
                        type="tool-call",
                        toolCallId=call["id"],
                        toolName=call["function"]["name"],
                        presentation=tool_presentation(
                            call["function"]["name"], presentations or {}
                        ),
                        args=arguments,
                        intent=intent,
                        status="cancelled",
                        awaitingUser=False,
                    )
                    tool_parts[call["id"]] = tool_part
            if message["role"] == "tool" and isinstance(row.metadata, ToolMessageMetadata):
                tool_message = message
                paired_part = tool_parts.get(tool_message["tool_call_id"])
                if paired_part is None:
                    raise RuntimeError("未闭合tool")
                tool_part = paired_part
                tool_part.update(project_tool_result(row.metadata))

        return tool_parts

    @staticmethod
    def rebuild(
        task: TaskRecord,
        runs: Sequence[ConversationRunRecord],
        context_rows: Sequence[ConversationTaskContextRecord],
        presentations: Mapping[str, ToolPresentation] | None = None,
    ) -> ConversationStateSnapshot:
        """从三类规范记录装配出通过校验的 Task 级 Transport 快照。

        参数:
            task: Task 级当前 Run 事实（``current_run_id`` 即当前活动）。
            runs: 属于 ``task`` 的全部 Run 记录。
            context_rows: 该 Task 已持久化的领域消息与 Transport 元数据。

        返回:
            新的 ``ConversationStateSnapshot``：消息按回合扁平排列（继承来的回合在前，本地回合按 Run
            的 ``created_at`` 再按 id、Run 内按 context 序号）；同一回合的 assistant 行合成一条回复
            ``assistant-turn-{回合号}``（见 ``_run_messages``）。按 Run 分组只为算 Run 级事实：
            已到终态的 Run 把结局写到它最后一条回复的 ``outcome``，当前 Run 给出 ``activity``
            （``turn`` 是它最后一个可见回合，与实时投影推进 ``activity.turn`` 的规则相同）。
            Run 身份不进快照。
            中断输出也保留展示；已落库文本均已停止生成，不由 Run 状态反推。

        异常:
            RuntimeError: 某条工具结果行在同一 Run 内找不到对应的 AI 工具调用。

        副作用:
            无。本方法不访问 Session、快照存储、工具、前端运行时状态或进程内 Transport registry。
        """

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

        messages: list[ConversationStateMessage] = []
        fork = task.extra.fork
        # No Run means copied context (sequence 0 is the fixed system prompt).
        # Visibility belongs to the row, including Agent forks; no history entity is queried.
        inherited_rows = [r for r in context_rows if r.run_id is None and r.sequence > 0]
        if inherited_rows:
            inherited = ConversationTaskStateRebuilder._run_messages(
                None, inherited_rows, presentations
            )
            messages.extend(inherited)
        activity: ConversationStateActivity | None = None
        for run in runs:
            rows = sorted(run_groups.get(run.id, []), key=lambda row: row.sequence)
            committed = [
                row
                for row in rows
                if row.project_to_transport
                and row.include_in_context
                and row.message["role"] == "assistant"
            ]
            last = committed[-1] if committed else None
            final_sequence = (
                last.sequence
                if last is not None
                and not last.message.get("tool_calls")
                and is_final_reply(
                    run.status,
                    cast(AssistantMessageMetadata, last.metadata).finish_reason,
                    content_to_text(last.message.get("content")),
                )
                else None
            )
            run_messages = ConversationTaskStateRebuilder._run_messages(
                run.id, rows, presentations, final_sequence
            )
            if run.status in TERMINAL_RUN_STATUSES:
                assistant = next(
                    (m for m in reversed(run_messages) if m["role"] == "assistant"), None
                )
                if assistant is not None:
                    # 终态后 ``updated_at`` 还可能被结构化输出改写结果再推一点，与实时
                    # 投影的终态时间差在秒级以内，耗时只供展示，不另存终态时间戳。
                    elapsed = run.updated_at - run.created_at
                    assistant["outcome"] = ConversationStateOutcome(
                        status=run.status,
                        endReason=run.end_reason,
                        error=ConversationTaskStateRebuilder.build_run_error(run),
                        usage=run.usage,
                        contextWindowTotal=run.context_window_total,
                        durationMs=max(0, int(elapsed.total_seconds() * 1000)),
                    )
            messages.extend(run_messages)
            if run.id == task.current_run_id:
                last_reply = next(
                    (m for m in reversed(run_messages) if m["role"] == "assistant"), None
                )
                activity = ConversationStateActivity(
                    modelActivity=None,
                    status=run.status,
                    turn=last_reply["turn"] if last_reply is not None else None,
                    resumable=run.status in _RESUMABLE_RUN_STATUSES
                    and not run_last_turn_withdrawn(rows),
                    startedAt=to_text(run.created_at),
                    endReason=run.end_reason,
                    error=ConversationTaskStateRebuilder.build_run_error(run),
                    usage=run.usage,
                    contextWindowTotal=run.context_window_total,
                    launch=run_launch_snapshot(run.model_config_id, run.options),
                )
        # 终端会话、看板、前台等待与交接都含进程内事实（看板要合并登记表的活条目），由 state service
        # 在重建后填入。
        snapshot = ConversationStateSnapshot(
            title=task.title,
            messages=messages,
            messageOffset=0,
            activity=activity,
            terminal_sessions=[],
            background_tasks=[],
            foreground_waits=[],
            handoffs=[],
        )

        if fork:
            launch = fork.initial_launch
            snapshot["fork"] = {
                "sourceTaskId": fork.source_task_id,
                "sourceTitle": fork.source_title,
                "taskType": cast(Literal["user_fork", "agent_fork"], task.task_type),
                "initialLaunch": {
                    "modelConfigId": launch.model_config_id,
                    "reasoningEffort": launch.options.reasoning_effort,
                    "disabledToolGroups": launch.options.disabled_tool_groups,
                },
            }
        return snapshot

    @staticmethod
    def _run_messages(
        run_id: int | None,
        rows: list[ConversationTaskContextRecord],
        presentations: Mapping[str, ToolPresentation] | None = None,
        final_sequence: int | None = None,
    ) -> list[ConversationStateMessage]:
        """把单个 Run（``run_id`` 为空时是继承来的历史）的 context 行按回合投影为消息。

        回合是行上记下的事实（``turn``，规则只在 ``RuntimeMessageContextManager._opens_turn``），
        这里不再判断「哪条输入开回合」：输入开头的回合投影为 user 消息加回复，没有输入开头的回合
        只有回复，回复 id 一律是 ``assistant-turn-{回合号}``。回合途中到达的 Agent 消息是回复的
        一段。不投影的行（撤回移出的历史、内部通知与结果）跳过。实时投影读同一事实（事件上的回合号）。

        由输入开启的回合总有回复（没有输出时是空回复），没有输入开头的回合只在有内容时才有：实时
        投影在并入输入时就建好回复、在第一次输出时才建没有输入开头的回复，两边规则相同。
        """

        tool_parts = ConversationTaskStateRebuilder.build_pair_tool_part(rows, presentations)
        if run_id is None:
            for part in tool_parts.values():
                part["awaitingUser"] = False
        turns: dict[int, list[ConversationTaskContextRecord]] = {}
        for row in rows:
            turns.setdefault(row.turn, []).append(row)
        run_messages: list[ConversationStateMessage] = []
        for turn, turn_rows in turns.items():
            opener = turn_rows[0]
            opener_metadata = opener.metadata
            opened_by_input = (
                opener.project_to_transport
                and opener.message["role"] == "user"
                and isinstance(opener_metadata, UserMessageMetadata)
                and opener_metadata.kind in TURN_INPUT_KINDS
            )
            if opened_by_input:
                # 只按展示绑定投影，不读 SDK 正文（正文是带 ``<user_input>`` 包装的模型输入）。
                input_metadata = cast(UserMessageMetadata, opener_metadata)
                parts, source = build_turn_input(
                    cast(UserInputPresentation, input_metadata.presentation),
                    input_metadata.source,
                )
                run_messages.append(
                    message(
                        user_message_id(cast(int, opener.message_id)),
                        "user",
                        list(parts),
                        turn=turn,
                        inherited=run_id is None,
                        source=source,
                    )
                )
            assistant = message(
                reply_id(turn), "assistant", [], turn=turn, inherited=run_id is None
            )
            for row in turn_rows[1:] if opened_by_input else turn_rows:
                if not row.project_to_transport:
                    continue
                row_message = row.message
                if row_message["role"] == "user":
                    user_metadata = cast(UserMessageMetadata, row.metadata)
                    # 只有一轮的输入（用户输入、Agent 消息）有展示形态；不开回合的只会是途中来信。
                    if user_metadata.kind in TURN_INPUT_KINDS:
                        assistant["parts"].append(
                            build_agent_message_part(
                                cast(int, row.message_id),
                                cast(UserInputPresentation, user_metadata.presentation),
                                cast(MessageSource, user_metadata.source),
                            )
                        )
                elif row_message["role"] == "assistant":
                    ConversationTaskStateRebuilder._append_assistant_parts(
                        assistant,
                        row_message,
                        tool_parts,
                        row.sequence,
                        row.sequence == final_sequence,
                    )
            if assistant["parts"] or opened_by_input:
                run_messages.append(assistant)
        return run_messages

    @staticmethod
    def _append_assistant_parts(
        assistant: ConversationStateMessage,
        ai_message: ChatCompletionAssistantMessageParam,
        tool_parts: dict[str, ConversationStateToolCallPart],
        source_sequence: int,
        final_reply: bool,
    ) -> None:
        # 只在正文非空时建 part：实时链路只在收到正文 delta 时才建 text part，而只带工具调用
        # 的 assistant 记录 ``content`` 常是 ``""``；冷重建若照样追加空 part，刷新后回合里会多出
        # 实时快照没有的空正文，把本应连续的调用序列切开。
        content = content_to_text(ai_message.get("content"))
        if content:
            assistant["parts"].append(
                ConversationStateTextPart(
                    type="text",
                    text=content,
                    status="completed",
                    sourceSequence=source_sequence,
                    finalReply=final_reply,
                )
            )
        calls = cast(
            list[ChatCompletionMessageFunctionToolCallParam],
            ai_message.get("tool_calls") or [],
        )
        for call in calls:
            tool_part = tool_parts.get(call["id"])
            # 中断输出里身份不全的调用：``build_pair_tool_part`` 不为它们建 part，实时链路同样
            # 没有 part，此处必须跳过——展示层缺口不得让冷重建整张快照失败。
            if tool_part is not None:
                assistant["parts"].append(tool_part)


__all__ = [
    "ConversationTaskStateRebuilder",
]
