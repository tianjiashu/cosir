"""delegate_task tool handler tests."""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from app.tools.schemas import ToolExecutionContext
from app.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies
from app.tools.tool_handler.delegate_task import build_delegate_task_definition
from app.tools.tool_models.delegate_task_args import DelegateTaskArgs


class FakeDelegateTaskExecutor:
    def __init__(self):
        self.called = False
        self.last_args: DelegateTaskArgs | None = None

    def execute(self, args: DelegateTaskArgs, execution_context):
        self.called = True
        self.last_args = args
        from app.tools.tool_execute.tool_success import tool_success

        return tool_success("delegate_task", "write", "child done")


def _valid_kwargs(**overrides):
    base = {
        "child_agent_id": "delegate_reviewer",
        "title": "Review diff",
        "objective": "review the diff",
        "rules": ["do not modify files"],
        "references": ["src/main.py"],
        "expected_output": "review comments",
    }
    base.update(overrides)
    return base


def test_delegate_task_definition_supports_parallel_scheduling():
    """delegate_task 同一回复内多个委派可工具级并行，但独立分组不与外部工具混并。

    子 Agent 并发的最终裁决仍在 delegation 业务层并发额度，调度器层并行不绕过该约束。
    """
    definition = build_delegate_task_definition()

    assert definition.parallel_mode == "parallel"
    assert definition.parallel_group == "delegate_task_group"


def test_delegate_task_requires_execution_context():
    tool = build_delegate_task_definition()

    result = tool.handler(**_valid_kwargs())

    assert result.status == "error"
    assert "execution context" in result.content


def test_delegate_task_requires_executor(tmp_path):
    tool = build_delegate_task_definition()
    context = ToolExecutionContext(
        task_id="task_1",
        workspace_id="workspace_1",
        workspace_root=tmp_path,
        turn_id="turn_parent",
    )

    result = tool.handler(**_valid_kwargs(), execution_context=context)

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

    result = tool.handler(**_valid_kwargs(), execution_context=context)

    assert result.status == "success"
    assert executor.called is True


def test_delegate_task_handler_passes_background_to_executor(tmp_path):
    """验证 handler 接收 background 参数并透传给执行器，避免运行时 TypeError。"""
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
        **_valid_kwargs(background="branch already rebased"),
        execution_context=context,
    )

    assert result.status == "success"
    assert executor.called is True
    assert executor.last_args is not None
    assert executor.last_args.background == "branch already rebased"


def test_delegate_task_args_requires_objective():
    with pytest.raises(ValidationError):
        DelegateTaskArgs.model_validate(
            {
                "child_agent_id": "delegate_reviewer",
                "rules": ["do not modify files"],
                "references": ["src/main.py"],
                "expected_output": "review comments",
            }
        )


def test_delegate_task_args_requires_rules():
    with pytest.raises(ValidationError):
        DelegateTaskArgs.model_validate(
            {
                "child_agent_id": "delegate_reviewer",
                "objective": "review the diff",
                "references": ["src/main.py"],
                "expected_output": "review comments",
            }
        )


def test_delegate_task_args_requires_references():
    with pytest.raises(ValidationError):
        DelegateTaskArgs.model_validate(
            {
                "child_agent_id": "delegate_reviewer",
                "objective": "review the diff",
                "rules": ["do not modify files"],
                "expected_output": "review comments",
            }
        )


def test_delegate_task_args_requires_expected_output():
    with pytest.raises(ValidationError):
        DelegateTaskArgs.model_validate(
            {
                "child_agent_id": "delegate_reviewer",
                "objective": "review the diff",
                "rules": ["do not modify files"],
                "references": ["src/main.py"],
            }
        )


def test_delegate_task_args_requires_child_agent_id():
    with pytest.raises(ValidationError):
        DelegateTaskArgs.model_validate(
            {
                "objective": "review the diff",
                "rules": ["do not modify files"],
                "references": ["src/main.py"],
                "expected_output": "review comments",
            }
        )


def test_delegate_task_args_requires_title():
    with pytest.raises(ValidationError):
        DelegateTaskArgs.model_validate(
            {
                "child_agent_id": "delegate_reviewer",
                "objective": "review the diff",
                "rules": ["do not modify files"],
                "references": ["src/main.py"],
                "expected_output": "review comments",
            }
        )


def test_delegate_task_args_rejects_blank_title():
    with pytest.raises(ValidationError):
        DelegateTaskArgs.model_validate(
            _valid_kwargs(title="   ")
        )


def test_delegate_task_args_rejects_too_many_rules():
    with pytest.raises(ValidationError):
        DelegateTaskArgs.model_validate(
            _valid_kwargs(rules=[f"rule {i}" for i in range(11)])
        )


def test_delegate_task_args_rejects_long_objective():
    with pytest.raises(ValidationError):
        DelegateTaskArgs.model_validate(
            _valid_kwargs(objective="x" * 2001)
        )


def test_delegate_task_args_rejects_long_background():
    with pytest.raises(ValidationError):
        DelegateTaskArgs.model_validate(
            _valid_kwargs(background="x" * 2001)
        )


def test_delegate_task_args_defaults_background_empty():
    args = DelegateTaskArgs.model_validate(_valid_kwargs())
    assert args.background == ""


def test_delegate_task_schema_describes_model_visible_arguments():
    """Verify that delegate_task exposes useful model-facing argument descriptions.

    参数:
        无。
    返回:
        无。
    异常:
        AssertionError: 当工具描述或任一参数描述缺失关键调用契约时由 pytest 抛出。
    副作用:
        构建 delegate_task 工具定义并读取 Pydantic JSON schema。
    """

    model_definition = build_delegate_task_definition().to_model_tool_definition()
    properties = model_definition["parameters"]["properties"]

    assert "separable from the current turn" in model_definition["description"]
    assert "cannot recursively delegate" in model_definition["description"]
    assert "delegate_reviewer" in properties["child_agent_id"]["description"]
    assert "delegate_analyst" in properties["child_agent_id"]["description"]
    assert "delegate_coder" in properties["child_agent_id"]["description"]

    # 新增结构化字段必须出现在 schema properties 中
    assert "objective" in properties
    assert "rules" in properties
    assert "references" in properties
    assert "expected_output" in properties
    assert "background" in properties

    # 旧字段已被移除
    assert "delegation_type" not in properties
    assert "prompt" not in properties
    assert "requested_tools" not in properties


def test_delegate_task_definition_injects_agent_summary():
    """Verify that agent_summary 被注入到工具描述中。"""
    definition = build_delegate_task_definition("summary X")
    assert "summary X" in definition.description
