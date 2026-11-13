"""冷重建：只依据规范会话事实（Session、Run、context 行）以纯函数装配 Session 级 Transport 快照。

与实时投影（``event_projection``）同源：回合、回复 id、结局与 ``activity`` 的规则两边一致，刷新后的
快照与实时推进出的快照同形。这里不访问 Session、快照存储、工具或进程内 registry；终端会话、看板、
前台等待、交接与排队消息含进程内或队列事实，由 state service 在重建后填入。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import groupby
from operator import attrgetter
from typing import cast

from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageFunctionToolCallParam,
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
    ForkKind,
    empty_snapshot,
)
from app.assistant_transport.state.message_identity import reply_id, user_message_id
from app.core.tool.call_intent import display_call_arguments
from app.core.tool.presentation import ToolPresentation
from app.models.conversation_input import MessageSource, UserInputPresentation
from app.models.run_record import RunError, RunRecord
from app.models.session_context import (
    SessionContextRecord,
    run_last_turn_withdrawn,
)
from app.models.enums.run_status import (
    RESUMABLE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
)
from app.models.message_metadata import (
    TURN_INPUT_KINDS,
    AssistantMessageMetadata,
    ToolMessageMetadata,
    UserMessageMetadata,
)
from app.models.session_record import SessionRecord
from app.utils.datetime_utils import to_text
from app.utils.message_content import content_to_text


def run_error(run: RunRecord) -> RunError | None:
    """把持久化的 Run 错误契约（``run_terminal_error`` 写入）投影为 Transport 错误契约。

    ``message`` 保留原值（模型 HTTP 失败时可能是响应 message）；Run 没有错误契约时返回 ``None``。
    这里是读持久化 JSON 的信任边界：受控字段被污染时显式失败，而不是把非法数据投影成面向用户的
    文案（空白文案会让前端渲染出没有内容的错误气泡）。

    异常:
        ValueError: 错误契约不是映射，或不是恰好 ``{code, message}`` 两个非空白字符串。
    """

    error = run.error
    if error is None:
        return None
    if not isinstance(error, dict):
        raise ValueError(f"run {run.id} carries a non-mapping error contract")
    code = error.get("code")
    text = error.get("message")
    if (
        set(error) != {"code", "message"}
        or not isinstance(code, str)
        or not code.strip()
        or not isinstance(text, str)
        or not text.strip()
    ):
        raise ValueError(f"run {run.id} carries a malformed error contract")
    return RunError(code=code, message=text)


def pair_tool_parts(
    rows: list[SessionContextRecord],
    presentations: Mapping[str, ToolPresentation],
) -> dict[str, ConversationStateToolCallPart]:
    """把一段 context 行（已按 ``sequence`` 排序）里的工具调用按调用 id 配成 tool-call part。

    AI 消息里的每个调用先以 ``cancelled`` 登记，再由同一 ``tool_call_id`` 的 tool 行补全真实状态、
    展示数据与错误；未收到结果的调用保持 ``cancelled``。展示数据只读 tool 行的
    ``metadata.display_data``，不按工具名、委派或子会话记录推断。调用 id 在 Session 内唯一，由模型响应
    持久化入口保证。

    异常:
        RuntimeError: tool 行找不到同一段里对应的 AI 工具调用（上下文被破坏）。
    """

    parts: dict[str, ConversationStateToolCallPart] = {}
    for row in rows:
        row_message = row.message
        if row_message["role"] == "assistant" and row_message.get("tool_calls"):
            calls = cast(
                list[ChatCompletionMessageFunctionToolCallParam], row_message["tool_calls"]
            )
            for call in calls:
                # 中断输出可能只有部分调用身份；尚未建立的工具预览不重建为 part。
                if not call["id"] or not call["function"]["name"]:
                    continue
                intent, arguments = display_call_arguments(call["function"]["arguments"])
                parts[call["id"]] = ConversationStateToolCallPart(
                    type="tool-call",
                    toolCallId=call["id"],
                    toolName=call["function"]["name"],
                    presentation=tool_presentation(call["function"]["name"], presentations),
                    args=arguments,
                    intent=intent,
                    status="cancelled",
                    awaitingUser=False,
                    # 冷重建没有运行中的调用，运行起点恒为空（与实时投影离开 running 后同形）。
                    startedAt=None,
                )
        if row_message["role"] == "tool" and isinstance(row.metadata, ToolMessageMetadata):
            part = parts.get(row_message["tool_call_id"])
            if part is None:
                raise RuntimeError("未闭合tool")
            part.update(project_tool_result(row.metadata))
            # 继承副本与撤回移出的调用不能作出决定：按行级规则纠正，不只看 metadata。
            part["awaitingUser"] = row.awaits_user
    return parts


def rebuild_snapshot(
    session: SessionRecord,
    runs: Sequence[RunRecord],
    context_rows: Sequence[SessionContextRecord],
    presentations: Mapping[str, ToolPresentation],
) -> ConversationStateSnapshot:
    """从 Session、它的全部 Run 与 context 行装配 Session 级快照。

    消息按回合扁平排列：继承来的回合在前，本地回合按 Run 的 ``created_at`` 再按 id、Run 内按 context
    序号；同一回合的 assistant 行合成一条回复 ``assistant-turn-{回合号}``（见 ``_turn_messages``）。
    按 Run 分组只为算 Run 级事实：已到终态的 Run 把结局写到它最后一条回复的 ``outcome``，当前 Run
    给出 ``activity``（``turn`` 是它最后一个可见回合，与实时投影推进 ``activity.turn`` 的规则相同）。
    Run 身份不进快照。中断输出也保留展示；已落库文本均已停止生成，不由 Run 状态反推。

    异常:
        RuntimeError: 某条工具结果行在同一 Run 内找不到对应的 AI 工具调用。
        ValueError: 某个 Run 的错误契约被污染（见 :func:`run_error`）。
    """

    # Session 系统提示词（``run_id=None``、序号 0）是模型上下文事实，不进快照；其余 ``run_id=None`` 的
    # 行是继承来的副本，可见性归行本身（含 Agent 分叉），不查任何历史实体。
    run_groups = {
        run_id: sorted(rows, key=attrgetter("sequence"))
        for run_id, rows in groupby(
            sorted((row for row in context_rows if row.run_id is not None), key=attrgetter("run_id")),
            key=attrgetter("run_id"),
        )
    }
    inherited_rows = [row for row in context_rows if row.run_id is None and row.sequence > 0]
    snapshot = empty_snapshot(session.title)
    messages = snapshot["messages"]
    messages.extend(_turn_messages(inherited_rows, presentations, inherited=True))
    for run in runs:
        rows = run_groups.get(run.id, [])
        run_messages = _turn_messages(
            rows, presentations, inherited=False, final_sequence=_final_sequence(run, rows)
        )
        messages.extend(run_messages)
        last_reply = next((m for m in reversed(run_messages) if m["role"] == "assistant"), None)
        is_current = run.id == session.current_run_id
        if (last_reply is None or run.status not in TERMINAL_RUN_STATUSES) and not is_current:
            continue
        error = run_error(run)
        if last_reply is not None and run.status in TERMINAL_RUN_STATUSES:
            # 终态后 ``updated_at`` 还可能被结构化输出改写结果再推一点，与实时投影的终态时间差在秒级
            # 以内，耗时只供展示，不另存终态时间戳。
            elapsed = run.updated_at - run.created_at
            last_reply["outcome"] = ConversationStateOutcome(
                status=run.status,
                endReason=run.end_reason,
                error=error,
                usage=run.usage,
                contextWindowTotal=run.context_window_total,
                durationMs=max(0, int(elapsed.total_seconds() * 1000)),
            )
        if is_current:
            snapshot["activity"] = ConversationStateActivity(
                modelActivity=None,
                status=run.status,
                turn=last_reply["turn"] if last_reply is not None else None,
                resumable=run.status in RESUMABLE_RUN_STATUSES
                and not run_last_turn_withdrawn(rows),
                startedAt=to_text(run.created_at),
                endReason=run.end_reason,
                error=error,
                usage=run.usage,
                contextWindowTotal=run.context_window_total,
                launch=run_launch_snapshot(run.model_config_id, run.options),
            )
    fork = session.extra.fork
    if fork:
        snapshot["fork"] = {
            "sourceSessionId": fork.source_session_id,
            "sourceTitle": fork.source_title,
            "sessionType": cast(ForkKind, session.session_type),
            "initialLaunch": run_launch_snapshot(
                fork.initial_launch.model_config_id, fork.initial_launch.options
            ),
        }
    return snapshot


def _final_sequence(
    run: RunRecord, rows: list[SessionContextRecord]
) -> int | None:
    """这个 Run 的最终回复正文所在的 context 序号；最后一条已提交的回复带工具调用或不算最终回复时为空。"""

    committed = [
        row
        for row in rows
        if row.project_to_transport and row.include_in_context and row.message["role"] == "assistant"
    ]
    if not committed:
        return None
    last = committed[-1]
    if last.message.get("tool_calls") or not is_final_reply(
        run.status,
        cast(AssistantMessageMetadata, last.metadata).finish_reason,
        content_to_text(last.message.get("content")),
    ):
        return None
    return last.sequence


def _turn_messages(
    rows: list[SessionContextRecord],
    presentations: Mapping[str, ToolPresentation],
    *,
    inherited: bool,
    final_sequence: int | None = None,
) -> list[ConversationStateMessage]:
    """把一个 Run（``inherited`` 时是继承来的历史）的 context 行按回合投影为消息。

    回合是行上记下的事实（``turn``，规则只在 ``RuntimeMessageContextManager._opens_turn``），这里不再
    判断「哪条输入开回合」：输入开头的回合投影为 user 消息加回复，没有输入开头的回合只有回复，回复
    id 一律是 ``assistant-turn-{回合号}``。回合途中到达的 Agent 消息是回复的一段。不投影的行（撤回
    移出的历史、内部通知与结果）跳过。

    由输入开启的回合总有回复（没有输出时是空回复），没有输入开头的回合只在有内容时才有：实时投影在
    并入输入时就建好回复、在第一次输出时才建没有输入开头的回复，两边规则相同。
    """

    tool_parts = pair_tool_parts(rows, presentations)
    turns: dict[int, list[SessionContextRecord]] = {}
    for row in rows:
        turns.setdefault(row.turn, []).append(row)
    turn_messages: list[ConversationStateMessage] = []
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
                cast(UserInputPresentation, input_metadata.presentation), input_metadata.source
            )
            turn_messages.append(
                message(
                    user_message_id(cast(int, opener.message_id)),
                    "user",
                    list(parts),
                    turn=turn,
                    inherited=inherited,
                    source=source,
                )
            )
        reply = message(reply_id(turn), "assistant", [], turn=turn, inherited=inherited)
        for row in turn_rows[1:] if opened_by_input else turn_rows:
            if not row.project_to_transport:
                continue
            row_message = row.message
            if row_message["role"] == "user":
                user_metadata = cast(UserMessageMetadata, row.metadata)
                # 只有回合输入（用户输入、Agent 消息）有展示形态；不开回合的只会是途中来信。
                if user_metadata.kind in TURN_INPUT_KINDS:
                    reply["parts"].append(
                        build_agent_message_part(
                            cast(int, row.message_id),
                            cast(UserInputPresentation, user_metadata.presentation),
                            cast(MessageSource, user_metadata.source),
                        )
                    )
            elif row_message["role"] == "assistant":
                _append_assistant_parts(
                    reply, row_message, tool_parts, row.sequence, row.sequence == final_sequence
                )
        if reply["parts"] or opened_by_input:
            turn_messages.append(reply)
    return turn_messages


def _append_assistant_parts(
    reply: ConversationStateMessage,
    ai_message: ChatCompletionAssistantMessageParam,
    tool_parts: dict[str, ConversationStateToolCallPart],
    source_sequence: int,
    final_reply: bool,
) -> None:
    # 只在正文非空时建 part：实时链路只在收到正文 delta 时才建 text part，而只带工具调用的 assistant
    # 记录 ``content`` 常是 ``""``；冷重建若照样追加空 part，刷新后回合里会多出实时快照没有的空正文，
    # 把本应连续的调用序列切开。
    content = content_to_text(ai_message.get("content"))
    if content:
        reply["parts"].append(
            ConversationStateTextPart(
                type="text",
                text=content,
                status="completed",
                sourceSequence=source_sequence,
                finalReply=final_reply,
            )
        )
    calls = cast(
        list[ChatCompletionMessageFunctionToolCallParam], ai_message.get("tool_calls") or []
    )
    for call in calls:
        # 中断输出里身份不全的调用：``pair_tool_parts`` 不为它们建 part，实时链路同样没有 part，此处
        # 必须跳过——展示层缺口不得让冷重建整张快照失败。
        tool_part = tool_parts.get(call["id"])
        if tool_part is not None:
            reply["parts"].append(tool_part)


__all__ = ["pair_tool_parts", "rebuild_snapshot", "run_error"]
