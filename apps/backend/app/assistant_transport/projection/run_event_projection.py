"""Run 生命周期事件到 snapshot mutation 的投影。"""

from collections.abc import Sequence

from app.assistant_transport.projection.snapshot_navigation import find_assistant_message, find_run
from app.assistant_transport.state.conversation_run_snapshot import run_launch_snapshot
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.config.logging.logger import log
from app.core.events import RunInitializedEvent, RunResultUpdatedEvent, RunStatusChangedEvent
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.models.enums.conversation_run_status import TERMINAL_RUN_STATUSES, ConversationRunStatus

# Run 状态迁移白名单：投影层只放行领域侧已确认的迁移，其余（陈旧、重复投递）一律丢弃。
# ``cancelled -> running`` 与 ``suspended -> running`` 是合法恢复 / 唤醒迁移，由
# ``ConversationRunStateService`` 在数据库条件更新成功之后才发出；若在此丢弃，snapshot 会永久
# 停在终态，后续 tool-call 创建事件随之被终态短路丢弃，最终在工具状态迁移事件上抛 KeyError 中断
# 整个 Run。``completed`` 不接受普通续跑，避免迟到事件把已结束的 Run 拉回 active。
_ALLOWED_RUN_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "running": frozenset({"running", "suspended", "completed", "failed", "cancelled"}),
    "suspended": frozenset({"suspended", "running", "cancelled"}),
    "cancelled": frozenset({"cancelled", "running"}),
    "failed": frozenset({"failed", "running"}),
    "completed": frozenset({"completed"}),
}


def _usage_mutations(
    state: ConversationStateSnapshot,
    run_index: int,
    usage_stats: ConversationRunUsageStats | None,
) -> list[ConversationStateMutation]:
    """将新增模型用量并入 Run 快照；缺失字段和较小值不回退已有事实。"""

    if usage_stats is None:
        return []
    incoming_usage = usage_stats.to_dict()
    if incoming_usage is None:
        return []
    current_usage = state["runs"][run_index]["usage"]
    merged_usage = {
        key: (
            current_usage[key]
            if current_usage is not None
            and (
                incoming_usage[key] is None
                or (current_usage[key] is not None and incoming_usage[key] < current_usage[key])
            )
            else incoming_usage[key]
        )
        for key in incoming_usage
    }
    if merged_usage == current_usage:
        return []
    return [ConversationStateMutation("set", ("runs", run_index, "usage"), merged_usage)]


def _empty_run(event: RunInitializedEvent) -> dict[str, object]:
    """返回待后续投影填充消息的空 Run 骨架。"""

    return {
        "runId": event.run_id,
        "status": "running",
        "endReason": None,
        "contextWindowTotal": event.context_window_total,
        "launch": run_launch_snapshot(event.model_config_id, event.options),
        "messages": [],
        "usage": None,
        "error": None,
    }


def plan_run_initialized(
    event: RunInitializedEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """建立 Run 骨架。

    普通初始化在已存在同一 Run 时回空列表（幂等），``replace_existing`` 用于编辑重跑重建骨架。
    丢弃骨架建立的快照分叉分支会写 WARNING 日志。
    """

    existing_index = next(
        (index for index, run in enumerate(state["runs"]) if run["runId"] == event.run_id),
        None,
    )
    if existing_index is not None:
        if not event.replace_existing:
            return []
        return [
            ConversationStateMutation("set", ("runs", existing_index), _empty_run(event)),
            ConversationStateMutation("set", ("current_run_id",), event.run_id),
            ConversationStateMutation("set", ("error",), None),
        ]

    current_run_id = state["current_run_id"]
    if event.run_id is None:
        return []
    if current_run_id is not None and (
        event.run_id <= current_run_id
        or not state["runs"]
        or state["runs"][-1]["status"] not in TERMINAL_RUN_STATUSES
    ):
        # 走到这里说明 snapshot 与 DB 已分叉（新 run 被当作陈旧事件丢弃）：不阻断
        # 主流程，但必须留痕，否则排障时只能看到僵尸现象看不到丢弃本身。
        log.warning(
            "transport_run_initialized_skeleton_dropped",
            extra={
                "msg": "RunInitializedEvent 未建立骨架：快照存在更新的未结束 run，疑似分叉",
                "data": {
                    "task_id": event.task_id,
                    "run_id": event.run_id,
                    "current_run_id": current_run_id,
                    "latest_run_status": state["runs"][-1]["status"] if state["runs"] else None,
                },
            },
        )
        return []
    return [
        ConversationStateMutation("set", ("runs", len(state["runs"])), _empty_run(event)),
        ConversationStateMutation("set", ("current_run_id",), event.run_id),
        ConversationStateMutation("set", ("error",), None),
    ]


def plan_run_status_changed(
    event: RunStatusChangedEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """更新 Run 状态、终态原因、错误契约与用量；终态时收口仍 running 的 text / reasoning part。

    目标状态不在白名单矩阵内时（陈旧或重复投递的迁移）返回空列表并留痕。

    异常:
        KeyError: 事件引用的 Run 不存在。
    """

    run_index = find_run(state, event.run_id)
    current_status = str(state["runs"][run_index]["status"])
    if event.status.value not in _ALLOWED_RUN_STATUS_TRANSITIONS.get(current_status, frozenset()):
        # 白名单丢弃对重复投递是常态，但若因投影失败丢掉了唯一一次合法迁移，
        # snapshot 会永久停在旧状态；留痕区分两种情况。
        log.warning(
            "transport_run_status_transition_dropped",
            extra={
                "msg": "RunStatusChangedEvent 不在迁移白名单内，投影被丢弃",
                "data": {
                    "task_id": event.task_id,
                    "run_id": event.run_id,
                    "snapshot_status": current_status,
                    "incoming_status": event.status.value,
                },
            },
        )
        return []
    mutations: list[ConversationStateMutation] = [
        ConversationStateMutation("set", ("runs", run_index, "status"), event.status.value),
        ConversationStateMutation("set", ("runs", run_index, "endReason"), event.end_reason),
        ConversationStateMutation("set", ("runs", run_index, "error"), event.error),
    ]
    mutations.extend(_usage_mutations(state, run_index, event.usage_stats))
    located = find_assistant_message(state, event.run_id, required=False)
    if located is None or event.status.value not in TERMINAL_RUN_STATUSES:
        return mutations
    _, assistant_index = located
    base = ("runs", run_index, "messages", assistant_index)
    for part_index, part in enumerate(
        state["runs"][run_index]["messages"][assistant_index]["parts"]
    ):
        if (
            isinstance(part, dict)
            and part.get("type") in {"text", "reasoning"}
            and part.get("status") == "running"
        ):
            mutations.append(
                ConversationStateMutation(
                    "set", (*base, "parts", part_index, "status"), "completed"
                )
            )
    return mutations


def plan_run_result_updated(
    event: RunResultUpdatedEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """只更新已完成 Run 的终态原因与用量；快照不是 completed 时丢弃并留痕。"""

    run_index = find_run(state, event.run_id)
    if state["runs"][run_index]["status"] != ConversationRunStatus.COMPLETED.value:
        log.warning(
            "transport_run_result_update_dropped",
            extra={
                "msg": "已完成 Run 的结果事件未命中 completed 快照",
                "data": {
                    "task_id": event.task_id,
                    "run_id": event.run_id,
                    "snapshot_status": state["runs"][run_index]["status"],
                },
            },
        )
        return []
    return [
        ConversationStateMutation("set", ("runs", run_index, "endReason"), event.end_reason),
        *_usage_mutations(state, run_index, event.usage_stats),
    ]
