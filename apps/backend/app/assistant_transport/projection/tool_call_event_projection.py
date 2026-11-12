"""工具调用生命周期事件到 snapshot mutation 的投影。"""

import copy
from collections.abc import Mapping, Sequence

from app.assistant_transport.projection.snapshot_navigation import (
    ensure_reply,
    find_tool,
    is_active,
)
from app.assistant_transport.projection.tool_presentation import tool_presentation
from app.assistant_transport.projection.tool_result import project_tool_result
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_part import ConversationStateToolCallPart
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.config.logging.logger import log
from app.core.events import (
    ToolCallCreatedEvent,
    ToolCallIntentEvent,
    ToolCallStatusChangedEvent,
)
from app.core.tool.presentation import ToolPresentation
from app.models.message_metadata import ToolMessageMetadata
from app.utils.datetime_utils import to_text

# 已终态的 part 只接受自迁移，以及「占位被真实结果覆盖」的 ``cancelled -> completed/failed``：
# ``RuntimeMessageContextManager.add_message`` 可能按调用身份用真实结果覆盖此前的取消占位，若在此丢弃，
# 数据库已是真实终态而 snapshot 会永久停在 ``cancelled``。``completed`` / ``failed`` 不可逆，迟到事件
# 不会把已结束的调用拉回 active。非终态（``pending`` / ``running``）可去往任何状态：``running -> pending``
# 是 HITL 工具首次调用返回等待占位（``ToolObservation.awaiting_decision``），用户决定后才推到终态。
_TERMINAL_TOOL_ACCEPTS: dict[str, frozenset[str]] = {
    "cancelled": frozenset({"cancelled", "completed", "failed"}),
    "failed": frozenset({"failed"}),
    "completed": frozenset({"completed"}),
}


def plan_tool_call_created(
    event: ToolCallCreatedEvent,
    state: ConversationStateSnapshot,
    presentations: Mapping[str, ToolPresentation],
) -> Sequence[ConversationStateMutation]:
    """在这一回合回复的尾部建立 ``pending`` 的 tool-call part。

    同名 toolCallId 已存在时回空列表（幂等）；当前活动已终态时回空列表。这一回合还没有回复时
    连同 part 一起建出回复。
    """

    if not is_active(state):
        return []
    if find_tool(state, event.tool_call_id) is not None:
        return []
    new_part: ConversationStateToolCallPart = {
        "type": "tool-call",
        "toolCallId": event.tool_call_id,
        "toolName": event.tool_name,
        "presentation": tool_presentation(event.tool_name, presentations),
        "status": "pending",
        "error": None,
        # 刚创建的 ``pending`` 是「准备执行」不是「等你操作」；后者由结果投影按占位行判定。
        "awaitingUser": False,
        "isError": False,
        # 运行起点只在 running 期间有值，见 ``plan_tool_call_status_changed``。
        "startedAt": None,
    }
    message_index, mutations = ensure_reply(state, event.turn, new_part)
    if message_index is None:
        return mutations
    parts = state["messages"][message_index]["parts"]
    return [
        *mutations,
        ConversationStateMutation(
            "set", ("messages", message_index, "parts", len(parts)), new_part
        ),
    ]


def plan_tool_call_intent(
    event: ToolCallIntentEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """意图与参数来自模型输出本身，拒绝、取消、HITL 都不依赖 running 事件，且与冷重建同源。"""
    found = find_tool(state, event.tool_call_id)
    if found is None:
        log.warning("tool_call_part_missing_for_intent", extra={
            "msg": "工具调用 part 缺失，跳过意图投影",
            "data": {"run_id": event.run_id, "tool_call_id": event.tool_call_id},
        })
        return []
    base, _ = found
    return [
        ConversationStateMutation("set", (*base, "intent"), event.intent),
        ConversationStateMutation("set", (*base, "args"), copy.deepcopy(event.args)),
    ]


def plan_tool_call_status_changed(
    event: ToolCallStatusChangedEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """把 tool-call part 迁到目标状态，并按需写入参数、错误与展示数据。

    part 不存在、或已终态的 part 收到不接受的迁移时，返回空列表并记 warning。
    """

    found = find_tool(state, event.tool_call_id)
    if found is None:
        # 状态事件不携带 toolName，缺少 part 时无法补建合法 part；此处只
        # 降级为「展示事实缺失」并继续后续投影，绝不让展示层缺口中断 Agent 执行。
        log.warning(
            "tool_call_part_missing_for_status_change",
            extra={
                "msg": "工具调用 part 缺失，跳过本次状态投影",
                "data": {
                    "run_id": event.run_id,
                    "tool_call_id": event.tool_call_id,
                    "status": event.status,
                },
            },
        )
        return []
    base, part = found
    current = part["status"]
    accepted = _TERMINAL_TOOL_ACCEPTS.get(current)
    if accepted is not None and event.status not in accepted:
        # 陈旧或重复投递的事件。必须留痕：静默丢弃会让「DB 已是真实终态、snapshot 停在旧终态」无从排查。
        log.warning(
            "tool_call_status_transition_dropped",
            extra={
                "msg": "已终态的工具调用收到不接受的迁移，已丢弃",
                "data": {
                    "task_id": event.task_id,
                    "run_id": event.run_id,
                    "tool_call_id": event.tool_call_id,
                    "from_status": current,
                    "to_status": event.status,
                },
            },
        )
        return []
    fields = project_tool_result(
        ToolMessageMetadata(
            status=event.status,
            error=event.error,
            display_data=event.display_data,
            team_run_id=event.team_run_id,
            background=event.background,
        )
    )
    display = fields["display_data"]
    current_display = part.get("display_data")
    if isinstance(current_display, dict) and current_display.get("kind") == "terminal-result":
        # 运行中的 terminal-result 只来自实时通道（启动时就建立，见 ``LiveOutputChannel.start``）。
        # 最终 ExecutionResult 的展示数据是权威值；实时通道可能尚未送达尾部。
        # 只有缺少结构化终态（例如执行器异常、没有输出就被取消）时才保留已经收到的命令与预览。
        if (
            display is not None
            and display.get("kind") == "terminal-result"
            and "output" not in display
        ):
            if "output" in current_display:
                display["output"] = current_display["output"]
        elif display is None or "kind" not in display:
            fields["display_data"] = {**copy.deepcopy(current_display), **(display or {})}
    mutations = [
        ConversationStateMutation("set", (*base, key), value) for key, value in fields.items()
    ]
    # ``startedAt`` 只表达「从何时起在运行」：进入 running 时取那次事件的发生时刻（投影器不读墙钟），
    # 重复投递的自迁移不改它；离开 running 时清空，结束的调用因此与冷重建（没有运行中的调用）一致。
    if event.status == "running" and current != "running":
        mutations.append(
            ConversationStateMutation("set", (*base, "startedAt"), to_text(event.occurred_at))
        )
    elif event.status != "running" and current == "running":
        mutations.append(ConversationStateMutation("set", (*base, "startedAt"), None))
    return mutations
