"""delegate AgentProfile tests."""

from pydantic import BaseModel

from app.config.configuration import build_agent_registry
from app.core.agents.agent_profile import default_developer_agent, developer_agent_pro
from app.core.delegation.child_agent_profile_builder import ChildAgentProfileBuilder
from app.models.turn_record import TurnRecord
from app.tools.schemas.tool_definition import ToolDefinition
from app.utils.datetime_utils import utc_now


class EmptyToolArgs(BaseModel):
    """测试用空工具参数模型。

    参数:
        无。
    返回:
        无。
    异常:
        无。
    副作用:
        无。
    """


def _noop_tool_handler():
    """测试用空工具处理器。

    参数:
        无。
    返回:
        None。
    异常:
        无。
    副作用:
        无。
    """

    return None


def test_delegate_profiles_are_registered():
    """Verify that all built-in delegate profiles are registered.

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当任一内置 delegate profile 未注册时由 pytest 抛出。

    副作用:
        构建一个仅用于测试的内存 AgentProfileRegistry。
    """

    registry = build_agent_registry()

    assert registry.resolve("delegate_reviewer") is not None
    assert registry.resolve("delegate_analyst") is not None
    assert registry.resolve("delegate_coder") is not None


def test_delegate_profile_permissions_match_v1_roles():
    """Verify that delegate tool permissions match their v1 role boundaries.

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当 profile 字段或工具权限不符合方案时由 pytest 抛出。

    副作用:
        构建一个仅用于测试的内存 AgentProfileRegistry。
    """

    registry = build_agent_registry()

    reviewer = registry.resolve("delegate_reviewer")
    analyst = registry.resolve("delegate_analyst")
    coder = registry.resolve("delegate_coder")

    assert reviewer is not None
    assert reviewer.role == "delegate-reviewer"
    assert reviewer.description == "只做代码审查，指出问题、风险和遗漏；不修改代码，不运行测试。"
    assert reviewer.allowed_tools == [
        "read_file",
        "list_directory",
        "search_files",
        "codegraph_explore",
        "codegraph_search",
        "codegraph_node",
        "codegraph_callers",
        "codegraph_callees",
        "codegraph_impact",
    ]
    assert reviewer.model_name == "deepseek-v4-flash"
    assert reviewer.context_policy == "text_only_v1"
    assert reviewer.max_steps == 60
    assert "write_file" not in reviewer.allowed_tools
    assert "patch" not in reviewer.allowed_tools
    assert "execute_terminal" not in reviewer.allowed_tools

    assert analyst is not None
    assert analyst.role == "delegate-analyst"
    assert analyst.description == "只做事实、代码和文档分析，形成结论与建议；不修改代码。"
    assert analyst.allowed_tools == [
        "read_file",
        "list_directory",
        "search_files",
        "web_search",
        "web_extract",
        "codegraph_explore",
        "codegraph_search",
        "codegraph_node",
        "codegraph_callers",
        "codegraph_callees",
        "codegraph_impact",
    ]
    assert analyst.max_steps == 80
    assert "web_search" in analyst.allowed_tools
    assert "web_extract" in analyst.allowed_tools
    assert "write_file" not in analyst.allowed_tools

    assert coder is not None
    assert coder.role == "delegate-coder"
    assert (
        coder.description
        == "在父 Agent 委派范围内进行代码开发、修复和验证，并保持改动聚焦、可测试、可审查。"
    )
    assert coder.allowed_tools == [
        "read_file",
        "list_directory",
        "search_files",
        "write_file",
        "patch",
        "delete",
        "execute_terminal",
        "codegraph_explore",
        "codegraph_search",
        "codegraph_node",
        "codegraph_callers",
        "codegraph_callees",
        "codegraph_impact",
    ]
    assert coder.max_steps == 120
    assert "write_file" in coder.allowed_tools
    assert "patch" in coder.allowed_tools
    assert "execute_terminal" in coder.allowed_tools

    for profile in (reviewer, analyst, coder):
        assert "delegate_task" not in profile.allowed_tools


def test_default_parent_profiles_can_see_delegate_task_but_children_cannot():
    """Verify that production parent profiles expose delegation without enabling recursion.

    参数:
        无。
    返回:
        无。
    异常:
        AssertionError: 当默认父 profile 缺失 delegate_task 或 child profile 暴露递归委派时抛出。
    副作用:
        构造内存 ToolDefinition 列表用于权限筛选断言。
    """

    tools = [
        ToolDefinition(
            name="read_file",
            description="read",
            permission="never",
            handler=_noop_tool_handler,
            args_model=EmptyToolArgs,
            risk_level="read",
        ),
        ToolDefinition(
            name="delegate_task",
            description="delegate",
            permission="ask",
            handler=_noop_tool_handler,
            args_model=EmptyToolArgs,
            risk_level="medium",
        ),
    ]

    for parent_profile in (default_developer_agent(), developer_agent_pro()):
        assert "delegate_task" in parent_profile.allowed_tools
        assert [tool.name for tool in parent_profile.select_tools(tools)] == [
            "read_file",
            "delegate_task",
        ]

    registry = build_agent_registry()
    for child_agent_id in ("delegate_reviewer", "delegate_analyst", "delegate_coder"):
        child_profile = registry.resolve(child_agent_id)
        assert child_profile is not None
        assert "delegate_task" not in child_profile.allowed_tools
        assert [tool.name for tool in child_profile.select_tools(tools)] == ["read_file"]


def test_child_profile_builder_does_not_mutate_registry_profile():
    """Verify that the builder derives a turn profile without changing the registry profile.

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当 builder 复用原对象或修改注册表 profile 时由 pytest 抛出。

    副作用:
        构建测试用 TurnRecord 与 AgentProfileRegistry。
    """

    registry = build_agent_registry()
    registry_profile = registry.resolve("delegate_reviewer")
    assert registry_profile is not None
    original_tools = list(registry_profile.allowed_tools)
    turn = TurnRecord(
        turn_id="child_turn",
        task_id="task_1",
        input_text="review",
        status="pending",
        created_at=utc_now(),
        updated_at=utc_now(),
        agent_id="delegate_reviewer",
    )

    child = ChildAgentProfileBuilder().build(
        registry_profile=registry_profile,
        turn=turn,
        effective_tools=("read_file",),
    )

    assert child is not registry_profile
    assert child.turn is turn
    assert child.allowed_tools == ["read_file"]
    assert registry_profile.allowed_tools == original_tools
