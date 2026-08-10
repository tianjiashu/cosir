"""delegate_task tool handler tests."""

from dataclasses import replace

from app.tools.schemas import ToolExecutionContext
from app.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies
from app.tools.tool_handler.delegation.delegate_task import build_delegate_task_definition
from app.tools.tool_models.delegate_task_args import DelegateTaskArgs


class FakeDelegateTaskExecutor:
    def __init__(self):
        self.called = False

    def execute(self, args: DelegateTaskArgs, execution_context):
        self.called = True
        from app.tools.tool_execute.tool_success import tool_success

        return tool_success("delegate_task", "write", "child done")


def test_delegate_task_requires_executor(tmp_path):
    tool = build_delegate_task_definition()
    context = ToolExecutionContext(
        task_id="task_1",
        workspace_id="workspace_1",
        workspace_root=tmp_path,
        turn_id="turn_parent",
    )

    result = tool.handler(
        child_agent_id="delegate_reviewer",
        delegation_type="review",
        prompt="review",
        requested_tools=["read_file"],
        execution_context=context,
    )

    assert result.status == "error"
    assert "delegate_task_executor" in result.content


def test_delegate_task_delegates_to_injected_executor(tmp_path):
    tool = build_delegate_task_definition()
    executor = FakeDelegateTaskExecutor()
    context = ToolExecutionContext(
        task_id="task_1",
        workspace_id="workspace_1",
        workspace_root=tmp_path,
        turn_id="turn_parent",
    )
    context = replace(
        context,
        runtime_dependencies=ToolRuntimeDependencies(delegate_task_executor=executor),
    )

    result = tool.handler(
        child_agent_id="delegate_reviewer",
        delegation_type="review",
        prompt="review",
        requested_tools=["read_file"],
        execution_context=context,
    )

    assert result.status == "success"
    assert executor.called is True
