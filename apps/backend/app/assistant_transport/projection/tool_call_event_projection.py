"""工具调用生命周期事件到 snapshot mutation 的投影。"""

import copy
from collections.abc import Sequence
from typing import cast

from app.assistant_transport.projection.snapshot_navigation import (
    active_run_id,
    ensure_assistant,
    find_tool,
    iter_parts,
)
from app.assistant_transport.projection.tool_result import project_tool_result
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_part import ConversationStateToolCallPart
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.config.logging.logger import log
from app.core.events import ToolCallCreatedEvent, ToolCallsSettledEvent, ToolCallStatusChangedEvent
from app.models.message_metadata import ToolMessageMetadata

# 工具状态迁移白名单：投影层只放行领域侧已确认的迁移，其余（陈旧、重复投递）丢弃并留痕。
# ``cancelled -> completed/failed`` 是「占位被真实结果覆盖」的合法迁移：
# ``RuntimeMessageContextManager.add_message`` 可能按调用身份用真实结果覆盖此前的取消占位；
# 若在此丢弃该迁移，数据库已是真实终态而 snapshot 会永久停在 ``cancelled``
# （与 Run 状态白名单的 ``cancelled -> running`` 例外同源）。``completed`` / ``failed``
# 仍不可逆，避免迟到事件把已结束的调用拉回 active。``running -> pending`` 是 HITL 工具首次调用
# 返回等待占位（``ToolObservation.awaiting_decision``）：用户决定后执行完成，才经 ``pending`` 推到终态。
_ALLOWED_TOOL_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"pending", "running", "completed", "failed", "cancelled"}),
    "running": frozenset({"running", "pending", "completed", "failed", "cancelled"}),
    "cancelled": frozenset({"cancelled", "completed", "failed"}),
    "failed": frozenset({"failed"}),
    "completed": frozenset({"completed"}),
}
_TERMINAL_TOOL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def plan_tool_call_created(
    event: ToolCallCreatedEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """在当前活动 Run 的助手消息尾部建立 ``pending`` 的 tool-call part。

    同名 toolCallId 已存在时回空列表（幂等）；Run 已终态或不是当前活动时回空列表。Run 还没有
    助手消息时连同 part 一起建出助手消息。
    """

    run_id = active_run_id(state, event.run_id)
    if run_id is None:
        return []
    if find_tool(state, event.tool_call_id) is not None:
        return []
    new_part: ConversationStateToolCallPart = {
        "type": "tool-call",
        "toolCallId": event.tool_call_id,
        "toolName": event.tool_name,
        "status": "pending",
        "error": None,
        "presentation": copy.deepcopy(event.presentation),
        "isError": False,
        "approvalRequestId": None,
    }
    message_index, created = ensure_assistant(state, run_id, new_part)
    if message_index is None:
        return created
    parts = state["messages"][message_index]["parts"]
    return [
        ConversationStateMutation(
            "set", ("messages", message_index, "parts", len(parts)), new_part
        )
    ]


def plan_tool_call_status_changed(
    event: ToolCallStatusChangedEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """把 tool-call part 迁到目标状态，并按需写入参数、错误与展示数据。

    part 不存在、或已终态的 part 收到白名单外的迁移时，返回空列表并记 warning。

    异常:
        ValueError: 非终态 part 收到白名单外的目标状态（事件生产者与状态机不一致，需暴露）。
    """

    base = find_tool(state, event.tool_call_id)
    if base is None:
        # 状态事件不携带 toolName / presentation，缺少 part 时无法补建合法 part；此处只
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
    _, message_index, _, part_index = base
    part = cast(
        ConversationStateToolCallPart,
        state["messages"][cast(int, message_index)]["parts"][cast(int, part_index)],
    )
    current = str(part["status"])
    allowed_targets = _ALLOWED_TOOL_STATUS_TRANSITIONS.get(current, frozenset())
    if event.status not in allowed_targets:
        if current in _TERMINAL_TOOL_STATUSES:
            # 已终态的 part 只接受自迁移与「占位覆盖」白名单；其余是陈旧或重复投递的事件。
            # 必须留痕：静默丢弃会让「DB 已是真实终态、snapshot 停在旧终态」无从排查。
            log.warning(
                "tool_call_status_transition_dropped",
                extra={
                    "msg": "工具状态迁移不在白名单内，已丢弃",
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
        raise ValueError(f"invalid tool transition {current} -> {event.status}")
    fields = project_tool_result(
        ToolMessageMetadata(
            status=event.status,
            error=event.error,
            display_data=event.display_data,
            team_run_id=event.team_run_id,
            deferred=event.deferred,
        )
    )
    display = fields["display_data"]
    current_display = part.get("display_data")
    if (
        "terminal_output_seq" in part
        and isinstance(current_display, dict)
        and current_display.get("kind") == "terminal-result"
    ):
        # 已实时流出终端输出的 part：实时预览独立累计完整输出，是展示上的权威来源。
        streamed_output = current_display.get("output")
        if display is not None and display.get("kind") == "terminal-result":
            # 成功终态的模型正文可能被预算截断，输出沿用实时累计值。
            if isinstance(streamed_output, str):
                display["output"] = streamed_output
        elif display is None or "kind" not in display:
            # 取消 / 失败观察按契约不带成功态 ``kind``（空 dict 或只有 status_hint），整体覆盖会
            # 清空已流出的输出：保留实时展示，只叠加终态自带的短提示。带明确 ``kind`` 的展示是
            # 权威结果，照常替换，不与旧形状拼接。
            fields["display_data"] = {**copy.deepcopy(current_display), **(display or {})}
    mutations = [
        ConversationStateMutation("set", (*base, key), value) for key, value in fields.items()
    ]
    if event.args is not None:
        mutations.append(
            ConversationStateMutation("set", (*base, "args"), copy.deepcopy(event.args))
        )
    return mutations


def plan_tool_calls_settled(
    event: ToolCallsSettledEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """把该 Run 下仍 ``pending`` / ``running`` 的 tool-call part 收束为终态，只迁移状态与错误。

    后台占位（``background``）不收束：它的工作在 Run 之外继续，状态只由结论并入或撤销结算（Run 被
    取消时撤销会把它推到 ``cancelled``）。
    """

    mutations: list[ConversationStateMutation] = []
    for base, part in iter_parts(state, event.run_id):
        if (
            not isinstance(part, dict)
            or part.get("type") != "tool-call"
            or part.get("status") not in {"pending", "running"}
            or part.get("background") is True
        ):
            continue
        mutations.extend(
            [
                ConversationStateMutation("set", (*base, "status"), event.status),
                ConversationStateMutation(
                    "set",
                    (*base, "error"),
                    "已取消" if event.status == "cancelled" else "执行异常",
                ),
                ConversationStateMutation("set", (*base, "isError"), event.status == "failed"),
            ]
        )
    return mutations
