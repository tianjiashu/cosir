"""基于运行时记录构建状态级 checkpoint 快照。"""

from dataclasses import dataclass
from typing import Any, Dict, List

from app.core.agents.profile import AgentProfile
from app.events.types import EventType, RuntimeEvent
from app.storage.records import datetime_to_text, StepRecord, TaskRecord


@dataclass(frozen=True)
class CheckpointDraft:
    """表示尚未持久化的 checkpoint 载荷。

    参数:
        summary: 简短的人类可读 checkpoint 摘要。
        snapshot: 可 JSON 序列化的运行时状态快照。

    返回:
        checkpoint 草稿值对象。

    异常:
        无。

    副作用:
        无。
    """

    summary: str
    snapshot: Dict[str, Any]


def build_checkpoint_snapshot(
    task: TaskRecord,
    steps: List[StepRecord],
    events: List[RuntimeEvent],
    stage: str,
    agent_profile: AgentProfile,
) -> CheckpointDraft:
    """为单个任务构建状态级 checkpoint 快照。

    参数:
        task: 当前已持久化的任务状态。
        steps: 与任务关联的已持久化步骤。
        events: 与任务关联的已持久化事件。
        stage: 产生该 checkpoint 的运行时阶段。
        agent_profile: 负责执行该任务的 Agent profile。

    返回:
        包含摘要与可 JSON 序列化快照的 checkpoint 草稿。

    异常:
        无。

    副作用:
        无。
    """

    tool_history = _extract_tool_history(events)
    summary = (
        f"{stage}: task={task.status}, steps={len(steps)}, "
        f"events={len(events)}, tools={len(tool_history)}"
    )
    return CheckpointDraft(
        summary=summary,
        snapshot={
            "snapshot_version": 1,
            "agent_stage": stage,
            "agent": agent_profile.to_dict(),
            "task": task.to_dict(),
            "context_summary": _build_context_summary(task, steps, events),
            "steps": [_step_to_dict(step) for step in steps],
            "tool_call_history": tool_history,
            "file_change_metadata": [],
            "event_count": len(events),
            "latest_event_type": events[-1].event_type if events else "",
        },
    )


def _build_context_summary(
    task: TaskRecord,
    steps: List[StepRecord],
    events: List[RuntimeEvent],
) -> str:
    """为 checkpoint 构建紧凑的上下文摘要。

    参数:
        task: 当前已持久化的任务状态。
        steps: 与任务关联的已持久化步骤。
        events: 与任务关联的已持久化事件。

    返回:
        任务输入与运行进度的简短摘要。

    异常:
        无。

    副作用:
        无。
    """

    return (
        f"input={task.input_text[:120]!r}; "
        f"status={task.status}; steps={len(steps)}; events={len(events)}"
    )


def _step_to_dict(step: StepRecord) -> Dict[str, Any]:
    """将步骤记录转换为 checkpoint 快照形式。

    参数:
        step: 待序列化的步骤记录。

    返回:
        可 JSON 序列化的步骤字典。

    异常:
        无。

    副作用:
        无。
    """

    return {
        "step_id": step.step_id,
        "turn_id": step.turn_id,
        "step_type": step.step_type,
        "status": step.status,
        "input_summary": step.input_summary,
        "output_summary": step.output_summary,
        "error": step.error,
        "created_at": datetime_to_text(step.created_at),
        "updated_at": datetime_to_text(step.updated_at),
    }


def _extract_tool_history(events: List[RuntimeEvent]) -> List[Dict[str, Any]]:
    """为 checkpoint 诊断提取与工具相关的事件历史。

    参数:
        events: 单个任务的已持久化运行时事件。

    返回:
        有序的工具相关事件摘要。

    异常:
        无。

    副作用:
        无。
    """

    tool_event_types = {
        EventType.TOOL_CALL_REQUESTED,
        EventType.TOOL_CALL_STARTED,
        EventType.TOOL_CALL_FINISHED,
        EventType.OBSERVATION_ADDED,
    }
    return [
        {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "created_at": datetime_to_text(event.created_at),
            "payload": event.payload,
        }
        for event in events
        if event.event_type in tool_event_types
    ]
