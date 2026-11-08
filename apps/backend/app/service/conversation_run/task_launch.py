"""Initial launch lookup for a branch that has not executed locally yet."""

from app.core.workflow.message_queue.message_bodies.user_input import RunLaunch
from app.models.task_record import TaskRecord


def initial_task_launch(task: TaskRecord) -> RunLaunch | None:
    """Read the frozen seed, independent of the source Task's continued existence.

    A model ID is historical provenance, not proof it still exists. Message preparation
    validates the chosen model before enqueue; user sends may choose a replacement.
    """
    fork = task.extra.fork
    if not fork:
        return None
    launch = fork.initial_launch
    if launch.model_config_id is None or launch.agent_id is None:
        return None
    return RunLaunch(
        agent_id=launch.agent_id, model_config_id=launch.model_config_id, options=launch.options
    )
