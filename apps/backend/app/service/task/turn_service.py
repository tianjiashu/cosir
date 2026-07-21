"""Turn orchestration service.

单一职责：编排轮次的创建与管理——创建轮次时同步更新所属任务的
最新轮次 ID 和消息预览。

职责边界：
- 负责：轮次创建（含任务最新轮次更新）、轮次查询与状态更新。
- 不负责：直接 SQL 操作（委托给 ``TurnCrud``/``TaskCrud``）。
"""

from typing import List

from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.turn_crud import TurnCrud
from app.models import TurnRecord
from app.utils.datetime_utils import preview


class TurnService:
    """Orchestrate turn creation, queries, and status management."""

    def __init__(self, task_crud: TaskCrud, turn_crud: TurnCrud) -> None:
        self._task = task_crud
        self._turn = turn_crud

    def create_turn(self, task_id: str, input_text: str, status: str = "pending") -> TurnRecord:
        """Create a turn and update the parent task's latest turn info."""
        if not isinstance(input_text, str) or not input_text.strip():
            raise ValueError("input_text must be a non-empty string")
        turn = self._turn.create(task_id, input_text, status)
        self._task.update_latest_turn(task_id, turn.turn_id, preview(input_text))
        return turn

    def get_turn(self, turn_id: str) -> TurnRecord:
        return self._turn.get(turn_id)

    def list_turns_for_task(self, task_id: str) -> List[TurnRecord]:
        return self._turn.list_by_task(task_id)

    def update_turn_status(self, turn_id: str, status: str) -> TurnRecord:
        return self._turn.update_status(turn_id, status)

    def claim_pending_turn(self, turn_id: str) -> bool:
        return self._turn.claim_pending(turn_id)

    def get_turn_for_task(self, task_id: str) -> TurnRecord:
        return self._turn.get_first_for_task(task_id)
