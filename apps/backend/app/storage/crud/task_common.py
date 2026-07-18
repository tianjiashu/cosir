"""SQLite task-store shared helpers and imports."""

from datetime import datetime, timezone
import json
from typing import List, Optional
from uuid import uuid4

from sqlalchemy import Select, asc, delete, func, select, update

from app.events.types import EventType, RuntimeEvent
from app.storage.model.task import EventModel, StepModel, TaskModel, TurnModel, WorkspaceModel
from app.storage.records import StepRecord, TaskRecord, TurnRecord, WorkspaceRecord


__all__ = [
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
    """杩斿洖褰撳墠 UTC datetime銆?"""

    return datetime.now(timezone.utc)


def _to_text(value: datetime) -> str:
    """灏?datetime 鍊煎簭鍒楀寲涓?ISO-8601 鏂囨湰銆?"""

    return value.isoformat()


def _from_text(value: str) -> datetime:
    """瑙ｆ瀽 ISO-8601 datetime 瀛楃涓层€?"""

    return datetime.fromisoformat(value)


def _events_for_task(task_id: str) -> Select[tuple[EventModel]]:
    """鏋勯€犱换鍔′簨浠舵煡璇㈣鍙ャ€?"""

    return select(EventModel).where(EventModel.task_id == task_id).order_by(asc(EventModel.sequence), asc(EventModel.created_at), asc(EventModel.event_id))


def _workspace_from_model(row: WorkspaceModel) -> WorkspaceRecord:
    """灏嗗伐浣滃尯 model 杞崲涓鸿褰曘€?"""

    return WorkspaceRecord(row.workspace_id, row.name, row.root_path, _from_text(row.created_at), _from_text(row.updated_at))


def _task_from_model(row: TaskModel) -> TaskRecord:
    """灏嗕换鍔?model 杞崲涓鸿褰曘€?"""

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
    """灏嗚疆娆?model 杞崲涓鸿褰曘€?"""

    return TurnRecord(row.turn_id, row.task_id, row.input_text, row.status, _from_text(row.created_at), _from_text(row.updated_at))


def _step_model(step: StepRecord) -> StepModel:
    """灏嗘楠よ褰曡浆鎹负 model銆?"""

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
    """灏嗘楠?model 杞崲涓鸿褰曘€?"""

    return StepRecord(row.step_id, row.turn_id, row.step_type, row.status, row.input_summary, row.output_summary, row.error, _from_text(row.created_at), _from_text(row.updated_at))


def _preview(value: str, limit: int = 80) -> str:
    """杩斿洖閫傚悎鏍囬鍜屼晶鏍忓睍绀虹殑鍗曡鎽樿銆?

    鍙傛暟:
        value: 鍘熷鐢ㄦ埛杈撳叆鏂囨湰銆?
        limit: 鎽樿鏈€澶у瓧绗︽暟銆?

    杩斿洖:
        鍘婚櫎澶氫綑绌虹櫧鍚庣殑鎽樿鏂囨湰锛岃秴闀挎椂杩藉姞鐪佺暐鍙枫€?

    寮傚父:
        鏃犮€?

    鍓綔鐢?
        鏃犮€?
    """

    normalized = " ".join(value.strip().split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1]}..."
