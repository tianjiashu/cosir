"""工具调用生命周期事件到 snapshot mutation 的投影。"""

import copy
from collections.abc import Sequence
from typing import cast

from app.assistant_transport.projection.snapshot_navigation import (
    find_assistant_message,
    find_run,
    find_tool,
)
from app.assistant_transport.projection.tool_result import project_tool_result
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_part import ConversationStateToolCallPart
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.config.logging.logger import log
from app.core.events import ToolCallCreatedEvent, ToolCallsSettledEvent, ToolCallStatusChangedEvent
from app.models.enums.conversation_run_status import TERMINAL_RUN_STATUSES
from app.models.message_metadata import ToolMessageMetadata

# 工具状态迁移白名单：投影层只放行领域侧已确认的迁移，其余（陈旧、重复投递）丢弃并留痕。
# ``cancelled -> completed/failed`` 是「占位被真实结果覆盖」的合法迁移：
# ``RuntimeMessageContextManager.add_message`` 可能按调用身份用真实结果覆盖此前的取消占位；
# 若在此丢弃该迁移，数据库已是真实终态而 snapshot 会永久停在 ``cancelled``
# （与 Run 状态白名单的 ``cancelled -> running`` 例外同源）。``completed`` / ``failed``
# 仍不可逆，避免迟到事件把已结束的调用拉回 active。
_ALLOWED_TOOL_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"pending", "running", "completed", "failed", "cancelled"}),
    "running": frozenset({"running", "completed", "failed", "cancelled"}),
    "cancelled": frozenset({"cancelled", "completed", "failed"}),
    "failed": frozenset({"failed"}),
    "completed": frozenset({"completed"}),
}
_TERMINAL_TOOL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def plan_tool_call_created(
    event: ToolCallCreatedEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """在 assistant 消息尾部建立 ``pending`` 的 tool-call part。

    同名 toolCallId 已存在时回空列表（幂等）；Run 已终态或 assistant 消息骨架缺失时回空列表，
    后者记 warning，不以异常中断 Run。
    """

    run_index = find_run(state, event.run_id)
    if state["runs"][run_index]["status"] in TERMINAL_RUN_STATUSES:
        return []
    located = find_assistant_message(state, event.run_id, required=False)
    if located is None:
        # assistant 骨架缺失属于展示事实不完整，不得以断言中断整个 Run。
        log.warning(
            "tool_call_part_creation_skipped_missing_assistant_message",
            extra={
                "msg": "assistant 消息骨架缺失，跳过 tool-call part 建立",
                "data": {"run_id": event.run_id, "tool_call_id": event.tool_call_id},
            },
        )
        return []
    _, message_index = located
    parts = state["runs"][run_index]["messages"][message_index]["parts"]
    if any(
        isinstance(part, dict) and part.get("toolCallId") == event.tool_call_id for part in parts
    ):
        return []
    return [
        ConversationStateMutation(
            "set",
            ("runs", run_index, "messages", message_index, "parts", len(parts)),
            {
                "type": "tool-call",
                "toolCallId": event.tool_call_id,
                "toolName": event.tool_name,
                "status": "pending",
                "error": None,
                "presentation": copy.deepcopy(event.presentation),
                "isError": False,
                "approvalRequestId": None,
            },
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

    located = find_tool(state, event.tool_call_id, required=False)
    if located is None:
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
    run_index, message_index, part_index = located
    part = cast(
        ConversationStateToolCallPart,
        state["runs"][run_index]["messages"][message_index]["parts"][part_index],
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
    base = ("runs", run_index, "messages", message_index, "parts", part_index)
    fields = project_tool_result(
        ToolMessageMetadata(
            status=event.status,
            error=event.error,
            display_data=event.display_data,
            team_run_id=event.team_run_id,
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
    """把该 Run 下仍 ``pending`` / ``running`` 的 tool-call part 收束为终态，只迁移状态与错误。"""

    mutations: list[ConversationStateMutation] = []
    run_index = find_run(state, event.run_id)
    for message_index, run_message in enumerate(state["runs"][run_index]["messages"]):
        for part_index, part in enumerate(run_message["parts"]):
            if (
                not isinstance(part, dict)
                or part.get("type") != "tool-call"
                or part.get("status") not in {"pending", "running"}
            ):
                continue
            base = ("runs", run_index, "messages", message_index, "parts", part_index)
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
