"""Run 生命周期事件到 snapshot mutation 的投影（Task 视角）。

最新 Run 投影到 ``activity``；Run 进入终态时把结局写到它最后一个可见回合（``activity.turn``）的
回复上，历史回复因此不必知道 Run 也能显示结局。事件不按 Run 号路由：Run 状态只经
``ConversationRunLedger.transition`` 发出，而每个调用点都在这个 Run 仍是当前活动时迁移（驱动内的
迁移早于执行者注销，挂起、唤醒、恢复与取消都在 Task 闸门内），新 Run 只在上一个驱动结束后开出。
"""

from collections.abc import Sequence
from typing import cast

from app.assistant_transport.projection.final_reply import is_final_reply
from app.assistant_transport.projection.snapshot_navigation import reply_index
from app.assistant_transport.state.conversation_state_activity import (
    ConversationStateActivity,
    run_launch_snapshot,
)
from app.assistant_transport.state.conversation_state_mutation import (
    ConversationStateMutation,
    SnapshotPath,
)
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.assistant_transport.state.conversation_state_usage import ConversationStateUsage
from app.config.logging.logger import log
from app.core.events import RunInitializedEvent, RunResultUpdatedEvent, RunStatusChangedEvent
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.models.enums.conversation_run_status import (
    TERMINAL_RUN_STATUSES,
    ConversationRunStatus,
    RunTransition,
)
from app.utils.datetime_utils import from_text, to_text

# Run 状态迁移白名单：投影层只放行领域侧已确认的迁移，其余（陈旧、重复投递）一律丢弃。
# ``cancelled -> running`` 与 ``suspended -> running`` 是合法恢复 / 唤醒迁移，由
# ``ConversationRunLedger`` 在数据库条件更新成功之后才发出；若在此丢弃，snapshot 会永久
# 停在终态，后续 tool-call 创建事件随之被终态短路丢弃。``completed`` 不接受普通续跑，避免迟到
# 事件把已结束的 Run 拉回 active。
# 能恢复的状态就是恢复迁移的来源状态：同一事实只取自迁移表。
_RESUMABLE_RUN_STATUSES = frozenset(status.value for status in RunTransition.RESUME.sources)

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
                incoming[key] is None or (current[key] is not None and incoming[key] < current[key])
            )
            else incoming[key]
        )
        for key in incoming
    }
    return None if merged == current else cast(ConversationStateUsage, merged)


def _outcome_path(message_index: int) -> SnapshotPath:
    return ("messages", message_index, "outcome")


def _duration_ms(started_at: str, event: RunStatusChangedEvent) -> int:
    """Run 创建到终态的毫秒数：两端都来自 Run 记录的必填时间戳。"""

    elapsed = event.changed_at - from_text(started_at)
    return max(0, int(elapsed.total_seconds() * 1000))


def plan_run_initialized(
    event: RunInitializedEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """新 Run 成为当前活动：还没有可见回合、不可恢复。

    重复投递由事件去重窗口吸收；快照在建 Run 提交后、投影前被懒重建时，重建出的活动与这里写入的
    值相同，直接覆盖无妨。
    """

    return [
        ConversationStateMutation(
            "set",
            ("activity",),
            ConversationStateActivity(
                modelActivity=None,
                status=ConversationRunStatus.RUNNING.value,
                turn=None,
                resumable=False,
                startedAt=to_text(event.created_at),
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
    """更新当前活动的状态、终态原因、错误、用量与可恢复判定，并维护当前回合回复的结局。

    进入终态时写 ``outcome`` 并收口仍 running 的正文，离开终态（恢复）时清空 ``outcome``。最终回复
    标记只在完成时按响应序号置真，不清除别处：completed 之后没有任何迁移，已置真的标记不会再变。
    这个 Run 还没有可见回合时不写结局（结局只在 ``activity`` 上）。不在白名单内的迁移（重复投递）
    返回空列表并留痕。
    """

    activity = state["activity"]
    if activity is None:
        _log_status_dropped(event, None)
        return []
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
        # 刚停下的 Run 都还没有被撤回，可恢复与否只看状态；撤回后的重建按行再判一次。
        ConversationStateMutation(
            "set", ("activity", "resumable"), target in _RESUMABLE_RUN_STATUSES
        ),
    ]
    if usage is not None:
        mutations.append(ConversationStateMutation("set", ("activity", "usage"), usage))
    if target != "running":
        mutations.append(ConversationStateMutation("set", ("activity", "modelActivity"), None))
    message_index = None if activity["turn"] is None else reply_index(state, activity["turn"])
    if message_index is None:
        return mutations
    reply = state["messages"][message_index]
    for part_index, part in enumerate(reply["parts"]):
        if (
            part["type"] == "text"
            and event.response_sequence is not None
            and part.get("sourceSequence") == event.response_sequence
            and is_final_reply(target, event.end_reason, part["text"])
        ):
            mutations.append(
                ConversationStateMutation(
                    "set", ("messages", message_index, "parts", part_index, "finalReply"), True
                )
            )
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
                    "durationMs": _duration_ms(activity["startedAt"], event),
                },
            )
        )
        for part_index, part in enumerate(reply["parts"]):
            if (
                isinstance(part, dict)
                and part.get("type") == "text"
                and part.get("status") == "running"
            ):
                mutations.append(
                    ConversationStateMutation(
                        "set",
                        ("messages", message_index, "parts", part_index, "status"),
                        "completed",
                    )
                )
    elif reply["outcome"] is not None:
        mutations.append(ConversationStateMutation("set", _outcome_path(message_index), None))
    return mutations


def _log_status_dropped(event: RunStatusChangedEvent, snapshot_status: str | None) -> None:
    # 白名单丢弃对重复投递是常态，但若因投影失败丢掉了唯一一次合法迁移，snapshot 会永久停在
    # 旧状态；留痕区分两种情况。
    log.warning(
        "transport_run_status_transition_dropped",
        extra={
            "msg": "RunStatusChangedEvent 不在迁移白名单内或快照没有当前活动，已丢弃",
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
    """只更新已完成的当前活动的终态原因与用量，并同步到它的结局；未完成时丢弃并留痕。

    结构化输出支线在同一次驱动内、完成迁移之后发出，这时 Run 仍是当前活动。
    """

    activity = state["activity"]
    if activity is None or activity["status"] != ConversationRunStatus.COMPLETED.value:
        _log_result_dropped(event, None if activity is None else activity["status"])
        return []
    usage = _merged_usage(activity["usage"], event.usage_stats)
    mutations = [ConversationStateMutation("set", ("activity", "endReason"), event.end_reason)]
    if usage is not None:
        mutations.append(ConversationStateMutation("set", ("activity", "usage"), usage))
    message_index = None if activity["turn"] is None else reply_index(state, activity["turn"])
    outcome = None if message_index is None else state["messages"][message_index]["outcome"]
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
            "msg": "已完成 Run 的结果事件未命中 completed 的当前活动",
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
