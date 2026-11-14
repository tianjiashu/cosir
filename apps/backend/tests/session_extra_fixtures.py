"""Typed Session metadata fixtures shared by lifecycle and fork tests."""

from app.models.session_extra import SessionExtra, SessionFork, SessionForkLaunch


def fork_extra(source_session_id: int, *, model_config_id: int | None = None) -> SessionExtra:
    return SessionExtra(
        fork=SessionFork(
            source_session_id=source_session_id,
            source_title="Source",
            tree_parent_session_id=source_session_id,
            initial_launch=SessionForkLaunch(
                agent_id="main_agent",
                model_config_id=model_config_id,
                reasoning_effort="low",
            ),
        )
    )

