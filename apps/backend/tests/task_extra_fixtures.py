"""Typed Task metadata fixtures shared by lifecycle and fork tests."""

from app.models.conversation_run_options import ConversationRunOptions
from app.models.task_extra import TaskExtra, TaskFork, TaskForkLaunch, TaskNodeConfig


def fork_extra(source_task_id: int, *, model_config_id: int | None = None) -> TaskExtra:
    return TaskExtra(
        fork=TaskFork(
            source_task_id=source_task_id,
            source_title="Source",
            tree_parent_task_id=source_task_id,
            initial_launch=TaskForkLaunch(
                agent_id="main_agent",
                model_config_id=model_config_id,
                options=ConversationRunOptions(reasoning_effort="low"),
            ),
        )
    )


def node_extra() -> TaskExtra:
    return TaskExtra(team_node=TaskNodeConfig(role="", system_prompt="", structured_output={}, model_settings={}))
