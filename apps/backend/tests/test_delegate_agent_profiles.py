"""delegate AgentProfile tests."""

from app.config.configuration import build_agent_registry
from app.core.delegation.child_agent_profile_builder import ChildAgentProfileBuilder
from app.models.turn_record import TurnRecord
from app.utils.datetime_utils import utc_now


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
    assert reviewer.model_name == "deepseek-v4-flash"
    assert reviewer.context_policy == "text_only_v1"
    assert reviewer.max_steps == 60
    assert "write_file" not in reviewer.allowed_tools
    assert "patch" not in reviewer.allowed_tools
    assert "execute_terminal" not in reviewer.allowed_tools

    assert analyst is not None
    assert analyst.role == "delegate-analyst"
    assert analyst.max_steps == 80
    assert "web_search" in analyst.allowed_tools
    assert "web_extract" in analyst.allowed_tools
    assert "write_file" not in analyst.allowed_tools

    assert coder is not None
    assert coder.role == "delegate-coder"
    assert coder.max_steps == 120
    assert "write_file" in coder.allowed_tools
    assert "patch" in coder.allowed_tools
    assert "execute_terminal" in coder.allowed_tools

    for profile in (reviewer, analyst, coder):
        assert "delegate_task" not in profile.allowed_tools


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
