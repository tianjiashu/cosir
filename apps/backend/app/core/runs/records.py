"""Durable run value objects."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class RunRecord:
    """Persisted state for one runtime run.

    Parameters:
        run_id: Unique run identifier.
        task_id: Associated task identifier.
        turn_id: Associated turn identifier.
        thread_id: Runtime thread identifier.
        status: Current run status.
        wait_reason: Optional wait reason.
        active_step_id: Optional active step identifier.
        active_wait_id: Optional active wait identifier.
        interruption_reason: Optional interruption reason.
        created_at: Creation timestamp.
        updated_at: Last update timestamp.

    Returns:
        Immutable run state record.

    Raises:
        None.

    Side effects:
        None.
    """

    run_id: str
    task_id: str
    turn_id: str
    thread_id: str
    status: str
    wait_reason: Optional[str]
    active_step_id: Optional[str]
    active_wait_id: Optional[str]
    interruption_reason: Optional[str]
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> Dict[str, Any]:
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
            "run_id": self.run_id,
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
