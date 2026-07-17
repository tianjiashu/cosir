"""SQLite task-store shared helpers and imports."""

from datetime import datetime, timezone
import json
from typing import List, Optional
from uuid import uuid4

from sqlalchemy import Select, asc, delete, func, select, update

from app.events.types import EventType, RuntimeEvent
from app.storage.model.task import CheckpointModel, EventModel, StepModel, TaskModel, TurnModel, WorkspaceModel
from app.storage.records import CheckpointRecord, StepRecord, TaskRecord, TurnRecord, WorkspaceRecord


__all__ = [
    "CheckpointModel",
    "CheckpointRecord",
    "EventModel",
    "EventType",
    "List",
    "Optional",
    "RuntimeEvent",
    "Select",
    "StepModel",
    "StepRecord",
    "TaskModel",
    "TaskRecord",
    "TurnModel",
    "TurnRecord",
    "WorkspaceModel",
    "WorkspaceRecord",
    "_checkpoint_from_model",
    "_events_for_task",
    "_from_text",
    "_preview",
    "_step_from_model",
    "_step_model",
    "_task_from_model",
    "_to_text",
    "_turn_from_model",
    "_utc_now",
    "_workspace_from_model",
    "asc",
    "datetime",
    "delete",
    "func",
    "json",
    "select",
    "update",
    "uuid4",
]


def _utc_now() -> datetime:
    """返回当前 UTC datetime。"""

    return datetime.now(timezone.utc)


def _to_text(value: datetime) -> str:
    """将 datetime 值序列化为 ISO-8601 文本。"""

    return value.isoformat()


def _from_text(value: str) -> datetime:
    """解析 ISO-8601 datetime 字符串。"""

    return datetime.fromisoformat(value)


def _events_for_task(task_id: str) -> Select[tuple[EventModel]]:
    """构造任务事件查询语句。"""

    return select(EventModel).where(EventModel.task_id == task_id).order_by(asc(EventModel.sequence), asc(EventModel.created_at), asc(EventModel.event_id))


def _workspace_from_model(row: WorkspaceModel) -> WorkspaceRecord:
    """将工作区 model 转换为记录。"""

    return WorkspaceRecord(row.workspace_id, row.name, row.root_path, _from_text(row.created_at), _from_text(row.updated_at))


def _task_from_model(row: TaskModel) -> TaskRecord:
    """将任务 model 转换为记录。"""

    return TaskRecord(
        task_id=row.task_id,
        workspace_id=row.workspace_id,
        agent_id=row.agent_id,
        input_text=row.input_text,
        title=row.title,
        last_message_preview=row.last_message_preview,
        latest_turn_id=row.latest_turn_id,
        status=row.status,
        created_at=_from_text(row.created_at),
        updated_at=_from_text(row.updated_at),
    )


def _turn_from_model(row: TurnModel) -> TurnRecord:
    """将轮次 model 转换为记录。"""

    return TurnRecord(row.turn_id, row.task_id, row.input_text, row.status, _from_text(row.created_at), _from_text(row.updated_at))


def _step_model(step: StepRecord) -> StepModel:
    """将步骤记录转换为 model。"""

    return StepModel(
        step_id=step.step_id,
        turn_id=step.turn_id,
        step_type=step.step_type,
        status=step.status,
        input_summary=step.input_summary,
        output_summary=step.output_summary,
        error=step.error,
        created_at=_to_text(step.created_at),
        updated_at=_to_text(step.updated_at),
    )


def _step_from_model(row: StepModel) -> StepRecord:
    """将步骤 model 转换为记录。"""

    return StepRecord(row.step_id, row.turn_id, row.step_type, row.status, row.input_summary, row.output_summary, row.error, _from_text(row.created_at), _from_text(row.updated_at))


def _checkpoint_from_model(row: CheckpointModel) -> CheckpointRecord:
    """将 checkpoint model 转换为记录。"""

    return CheckpointRecord(row.checkpoint_id, row.task_id, row.stage, row.summary, json.loads(row.snapshot_json), _from_text(row.created_at))


def _preview(value: str, limit: int = 80) -> str:
    """返回适合标题和侧栏展示的单行摘要。

    参数:
        value: 原始用户输入文本。
        limit: 摘要最大字符数。

    返回:
        去除多余空白后的摘要文本，超长时追加省略号。

    异常:
        无。

    副作用:
        无。
    """

    normalized = " ".join(value.strip().split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1]}…"
