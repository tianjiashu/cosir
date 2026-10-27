"""Run 生命周期事件到 snapshot mutation 的投影（Task 视角）。

最新 Run 投影到 ``activity``；Run 进入终态时把结局同步写到它最后一条助手消息的 ``outcome``，
历史回复因此不必按 Run 分组也能显示结局。事件按 ``run_id`` 路由：是 ``activity.runId`` 才更新
``activity``，旧 Run 的迟到事件只更新它那条消息的 ``outcome``。
"""

from collections.abc import Sequence
from typing import cast

from app.assistant_transport.projection.snapshot_navigation import (
    is_current_run,
    last_assistant_index,
)
from app.assistant_transport.state.conversation_state_activity import (
    ConversationStateActivity,
    run_launch_snapshot,
)
from app.assistant_transport.state.conversation_state_message import ConversationStateOutcome
from app.assistant_transport.state.conversation_state_mutation import (
    ConversationStateMutation,
    SnapshotPath,
)
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.assistant_transport.state.conversation_state_usage import ConversationStateUsage
from app.config.logging.logger import log
from app.core.events import RunInitializedEvent, RunResultUpdatedEvent, RunStatusChangedEvent
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.models.enums.conversation_run_status import TERMINAL_RUN_STATUSES, ConversationRunStatus

# Run 状态迁移白名单：投影层只放行领域侧已确认的迁移，其余（陈旧、重复投递）一律丢弃。
# ``cancelled -> running`` 与 ``suspended -> running`` 是合法恢复 / 唤醒迁移，由
# ``ConversationRunLedger`` 在数据库条件更新成功之后才发出；若在此丢弃，snapshot 会永久
# 停在终态，后续 tool-call 创建事件随之被终态短路丢弃。``completed`` 不接受普通续跑，避免迟到
# 事件把已结束的 Run 拉回 active。
_ALLOWED_RUN_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "running": frozenset({"running", "suspended", "completed", "failed", "cancelled"}),
    "suspended": frozenset({"suspended", "running", "cancelled"}),
    "cancelled": frozenset({"cancelled", "running"}),
    "failed": frozenset({"failed", "running"}),
    "completed": frozenset({"completed"}),
}


def _merged_usage(
    current: ConversationStateUsage | None,
    usage_stats: ConversationRunUsageStats | None,
) -> ConversationStateUsage | None:
    """把新增模型用量并入已有用量；缺失字段和较小值不回退已有事实。没有变化时返回 ``None``。"""

    if usage_stats is None:
        return None
    incoming = usage_stats.to_dict()
    if incoming is None:
        return None
    merged = {
        key: (
            current[key]
            if current is not None
            and (
                incoming[key] is None
                or (current[key] is not None and incoming[key] < current[key])
            )
            else incoming[key]
        )
        for key in incoming
    }
    return None if merged == current else cast(ConversationStateUsage, merged)


def _outcome_path(message_index: int) -> SnapshotPath:
    return ("messages", message_index, "outcome")


def plan_run_initialized(
    event: RunInitializedEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """新 Run 成为当前活动。

    同一 Run 重复初始化回空列表（幂等）。快照里的当前活动比事件更新，或仍未结束时丢弃并留痕：
    说明 snapshot 与 DB 已分叉，新 Run 被当作陈旧事件丢弃，不阻断主流程。
    """

    if event.run_id is None:
        return []
    activity = state["activity"]
    if activity is not None and activity["runId"] == event.run_id:
        return []
    if activity is not None and (
        event.run_id < activity["runId"] or activity["status"] not in TERMINAL_RUN_STATUSES
    ):
        log.warning(
            "transport_run_initialized_dropped",
            extra={
                "msg": "RunInitializedEvent 未切换当前活动：快照存在更新或未结束的 Run，疑似分叉",
                "data": {
                    "task_id": event.task_id,
                    "run_id": event.run_id,
                    "activity_run_id": activity["runId"],
                    "activity_status": activity["status"],
                },
            },
        )
        return []
    return [
        ConversationStateMutation(
            "set",
            ("activity",),
            ConversationStateActivity(
                runId=event.run_id,
                status=ConversationRunStatus.RUNNING.value,
                endReason=None,
                error=None,
                usage=None,
                contextWindowTotal=event.context_window_total,
                launch=run_launch_snapshot(event.model_config_id, event.options),
            ),
        )
    ]


def plan_run_status_changed(
    event: RunStatusChangedEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """更新 Run 状态、终态原因、错误与用量，并维护最后一条助手消息的结局。

    当前活动：按白名单迁移 ``activity``；进入终态时写 ``outcome`` 并收口仍 running 的
    text / reasoning part，离开终态（恢复）时清空 ``outcome``。旧 Run：只更新已有 ``outcome``。
    不在白名单内的迁移（陈旧或重复投递）返回空列表并留痕。
    """

    if not is_current_run(state, event.run_id):
        return _plan_historical_status(event, state)
    # is_current_run 已保证当前活动存在。
    activity = cast(ConversationStateActivity, state["activity"])
    current_status = activity["status"]
    target = event.status.value
    if target not in _ALLOWED_RUN_STATUS_TRANSITIONS.get(current_status, frozenset()):
        _log_status_dropped(event, current_status)
        return []
    usage = _merged_usage(activity["usage"], event.usage_stats)
    mutations = [
        ConversationStateMutation("set", ("activity", "status"), target),
        ConversationStateMutation("set", ("activity", "endReason"), event.end_reason),
        ConversationStateMutation("set", ("activity", "error"), event.error),
    ]
    if usage is not None:
        mutations.append(ConversationStateMutation("set", ("activity", "usage"), usage))
    message_index = last_assistant_index(state, event.run_id)
    if message_index is None:
        return mutations
    if target in TERMINAL_RUN_STATUSES:
        mutations.append(
            ConversationStateMutation(
                "set",
                _outcome_path(message_index),
                {
                    "status": target,
                    "endReason": event.end_reason,
                    "error": event.error,
                    "usage": usage if usage is not None else activity["usage"],
                    "contextWindowTotal": activity["contextWindowTotal"],
                },
            )
        )
        for part_index, part in enumerate(state["messages"][message_index]["parts"]):
            if (
                isinstance(part, dict)
                and part.get("type") in {"text", "reasoning"}
                and part.get("status") == "running"
            ):
                mutations.append(
                    ConversationStateMutation(
                        "set",
                        ("messages", message_index, "parts", part_index, "status"),
                        "completed",
                    )
                )
    elif state["messages"][message_index]["outcome"] is not None:
        mutations.append(ConversationStateMutation("set", _outcome_path(message_index), None))
    return mutations


def _plan_historical_status(
    event: RunStatusChangedEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """旧 Run 的迟到状态事件：只更新它已写出的结局，不碰当前活动。"""

    message_index = last_assistant_index(state, event.run_id)
    outcome = None if message_index is None else state["messages"][message_index]["outcome"]
    if message_index is None or outcome is None:
        _log_status_dropped(event, None)
        return []
    target = event.status.value
    if target not in TERMINAL_RUN_STATUSES or target not in _ALLOWED_RUN_STATUS_TRANSITIONS.get(
        outcome["status"], frozenset()
    ):
        _log_status_dropped(event, outcome["status"])
        return []
    usage = _merged_usage(outcome["usage"], event.usage_stats)
    updated: ConversationStateOutcome = {
        "status": target,
        "endReason": event.end_reason,
        "error": event.error,
        "usage": usage if usage is not None else outcome["usage"],
        "contextWindowTotal": outcome["contextWindowTotal"],
    }
    if updated == outcome:
        return []
    return [ConversationStateMutation("set", _outcome_path(message_index), updated)]


def _log_status_dropped(event: RunStatusChangedEvent, snapshot_status: str | None) -> None:
    # 白名单丢弃对重复投递是常态，但若因投影失败丢掉了唯一一次合法迁移，snapshot 会永久停在
    # 旧状态；留痕区分两种情况。
    log.warning(
        "transport_run_status_transition_dropped",
        extra={
            "msg": "RunStatusChangedEvent 不在迁移白名单内或找不到投影目标，已丢弃",
            "data": {
                "task_id": event.task_id,
                "run_id": event.run_id,
                "snapshot_status": snapshot_status,
                "incoming_status": event.status.value,
            },
        },
    )


def plan_run_result_updated(
    event: RunResultUpdatedEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """只更新已完成 Run 的终态原因与用量（当前活动与其结局同步更新）；未完成时丢弃并留痕。"""

    message_index = last_assistant_index(state, event.run_id)
    outcome = None if message_index is None else state["messages"][message_index]["outcome"]
    mutations: list[ConversationStateMutation] = []
    if is_current_run(state, event.run_id):
        activity = cast(ConversationStateActivity, state["activity"])
        if activity["status"] != ConversationRunStatus.COMPLETED.value:
            _log_result_dropped(event, activity["status"])
            return []
        usage = _merged_usage(activity["usage"], event.usage_stats)
        mutations.append(
            ConversationStateMutation("set", ("activity", "endReason"), event.end_reason)
        )
        if usage is not None:
            mutations.append(ConversationStateMutation("set", ("activity", "usage"), usage))
    elif outcome is None or outcome["status"] != ConversationRunStatus.COMPLETED.value:
        _log_result_dropped(event, None if outcome is None else outcome["status"])
        return []
    if message_index is not None and outcome is not None:
        outcome_usage = _merged_usage(outcome["usage"], event.usage_stats)
        mutations.append(
            ConversationStateMutation(
                "set",
                _outcome_path(message_index),
                {
                    **outcome,
                    "endReason": event.end_reason,
                    "usage": outcome_usage if outcome_usage is not None else outcome["usage"],
                },
            )
        )
    return mutations


def _log_result_dropped(event: RunResultUpdatedEvent, snapshot_status: str | None) -> None:
    log.warning(
        "transport_run_result_update_dropped",
        extra={
            "msg": "已完成 Run 的结果事件未命中 completed 快照",
            "data": {
                "task_id": event.task_id,
                "run_id": event.run_id,
                "snapshot_status": snapshot_status,
            },
        },
    )


__all__ = [
    "plan_run_initialized",
    "plan_run_result_updated",
    "plan_run_status_changed",
]
