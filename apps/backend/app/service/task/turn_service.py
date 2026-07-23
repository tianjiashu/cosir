"""Turn orchestration service.

单一职责：编排轮次的创建与管理——创建轮次时同步更新所属任务的
最新轮次 ID 和消息预览；并透传每轮消息轨迹的读写（跨轮记忆）。

职责边界：
- 负责：轮次创建（含任务最新轮次更新）、轮次查询与状态更新、消息轨迹读写透传。
- 不负责：直接 SQL 操作（委托给 ``TurnCrud``/``TaskCrud``/``TurnMessageCrud``）。
"""

from app.models import RuntimeMessage, TurnRecord
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.turn_crud import TurnCrud
from app.storage.crud.turn_message_crud import TurnMessageCrud
from app.utils.datetime_utils import preview


class TurnService:
    """Orchestrate turn creation, queries, status management, and message store."""

    def __init__(
        self,
        task_crud: TaskCrud,
        turn_crud: TurnCrud,
        message_crud: TurnMessageCrud | None = None,
    ) -> None:
        self._task = task_crud
        self._turn = turn_crud
        self._message = message_crud

    def create_turn(
        self,
        task_id: str,
        input_text: str,
        status: str = "pending",
        agent_id: str | None = None,
    ) -> TurnRecord:
        """Create a turn and update the parent task's latest turn info.

        参数:
            task_id: 所属任务标识。
            input_text: 本轮用户输入文本。
            status: 初始状态，默认 ``"pending"``。
            agent_id: 可选，本轮回绑定的 agent 标识；为 None 时回退到任务默认归属。
        """

        if not isinstance(input_text, str) or not input_text.strip():
            raise ValueError("input_text must be a non-empty string")
        turn = self._turn.create(task_id, input_text, status, agent_id=agent_id)
        self._task.update_latest_turn(task_id, turn.turn_id, preview(input_text))
        return turn

    def get_turn(self, turn_id: str) -> TurnRecord:
        return self._turn.get(turn_id)

    def list_turns_for_task(self, task_id: str) -> list[TurnRecord]:
        return self._turn.list_by_task(task_id)

    def get_turn_for_task(self, task_id: str) -> TurnRecord:
        return self._turn.get_latest_turn(task_id)

    def get_latest_turn(self, task_id: str) -> TurnRecord:
        return self._turn.get_latest_turn(task_id)

    def update_turn_status(
        self, turn_id: str, status: str, end_reason: str | None = None
    ) -> TurnRecord:
        return self._turn.update_status(turn_id, status, end_reason)

    def update_turn_response(self, turn_id: str, response_text: str | None) -> TurnRecord:
        """把轮次的 Agent 回复文本落库，供历史接口直接读取。"""

        return self._turn.update_response(turn_id, response_text)

    def has_turn_status(self, turn_id: str, status: str) -> bool:
        """Return whether the turn currently has the requested status."""

        return self._turn.get(turn_id).status == status

    def claim_pending_turn(self, turn_id: str) -> bool:
        return self._turn.claim_pending(turn_id)

    def save_turn_messages(self, turn_id: str, messages: list[RuntimeMessage]) -> None:
        """Persist a turn's ordered message trajectory (cross-turn memory)."""

        if self._message is None:
            return
        self._message.save_messages(turn_id, messages)

    def load_turn_messages(self, turn_id: str) -> list[RuntimeMessage]:
        """Load a turn's ordered message trajectory; empty list if none stored."""

        if self._message is None:
            return []
        return self._message.load_messages(turn_id)
