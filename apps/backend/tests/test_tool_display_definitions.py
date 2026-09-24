"""Static Tool UI declarations for long-running tool shells."""

from app.core.tools.tool_handler.child_task.child_agent_create import DelegateTaskTool
from app.core.tools.tool_handler.child_task.child_agent_send import ChildAgentSendTool
from app.core.tools.tool_handler.child_task.child_agent_status import ChildAgentStatusTool
from app.core.tools.tool_handler.child_task.child_agent_wait import ChildAgentWaitTool


def test_child_agent_tools_are_expandable_trace_rows(monkeypatch) -> None:
    """Child Agent activity stays groupable while its safe details remain inspectable."""

    monkeypatch.setattr(
        "app.config.configuration.get_agent_registry",
        lambda: type("Registry", (), {"child_agent_summary": lambda self: "reviewer"})(),
    )
    definitions = [
        DelegateTaskTool.to_definition(object.__new__(DelegateTaskTool)),
        ChildAgentSendTool.to_definition(object.__new__(ChildAgentSendTool)),
        ChildAgentStatusTool.to_definition(object.__new__(ChildAgentStatusTool)),
        ChildAgentWaitTool.to_definition(object.__new__(ChildAgentWaitTool)),
    ]

    for definition in definitions:
        display = definition.display
        assert display is not None
        assert display.surface == "trace"
        assert display.expandable is True
        assert display.expand_layout == "details"

    assert definitions[0].display.show_result is False
    assert all(definition.display.show_result is True for definition in definitions[1:])
