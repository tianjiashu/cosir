"""Static Tool UI declarations for long-running tool shells."""

from app.core.tools.tool_handler.child_task.child_agent_create import build_delegate_task_definition


def test_delegate_task_is_a_non_expandable_standalone_shell() -> None:
    """delegate_task exposes only its static title/icon during the parent run."""

    display = build_delegate_task_definition().display

    assert display is not None
    assert display.surface == "standalone"
    assert display.expandable is False
    assert display.expand_layout == "none"
    assert display.show_result is False
