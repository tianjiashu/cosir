"""Durable run state value object.

单一职责：承载一次 tool_execute 运行的不可变持久化状态（值对象）。
不负责数据库操作（由 ``storage/crud/durable_crud`` 负责）。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class RunRecord:
    """Persisted state for one tool_execute run.

    Parameters:
        run_id: Unique run identifier.
        task_id: Associated task identifier.
        turn_id: Associated turn identifier.
        thread_id: Runtime thread identifier.
        status: Current run status.
        wait_reason: Optional wait reason.
        active_step_id: Optional active step identifier.
        active_wait_id: Optional active wait identifier.
        interruption_reason: Optional reason why the run entered a terminal state
            (e.g. cancelled, agent profile unavailable, failure). This is a
            run-status-reason concern and is unrelated to LangGraph ``interrupt()``,
            which gates tool approval at the graph control-flow level and is carried
            by the checkpoint, not by this field.
        created_at: Creation timestamp.
        updated_at: Last update timestamp.

    Returns:
        Immutable run state record.

    Raises:
        None.

    Side effects:
        None.
    """

    task_id: str
    turn_id: str
    thread_id: str
    status: str
    wait_reason: str | None
    active_step_id: str | None
    active_wait_id: str | None
    interruption_reason: str | None
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary.

        Parameters:
            None.

        Returns:
            Dictionary containing run state fields.

        Raises:
            None.

        Side effects:
            None.
        """

        return {
            "task_id": self.task_id,
            "turn_id": self.turn_id,
            "thread_id": self.thread_id,
            "status": self.status,
            "wait_reason": self.wait_reason,
            "active_step_id": self.active_step_id,
            "active_wait_id": self.active_wait_id,
            "interruption_reason": self.interruption_reason,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
