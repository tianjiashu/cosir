"""AgentProfile 作为能力事实源的单元测试。

覆盖 Task 1「AgentProfile 成为能力事实源」的核心契约：
- ``goal`` 字段已移除；
- ``to_tool_summary()`` 仅暴露约定的 7 个能力字段，且由 ``allowed_tools`` 渲染工具摘要；
- ``prompt_ref`` 默认 None，传入时正确存储并序列化；
- 默认父 profile 的 ``capabilities``/``constraints`` 为 list 而非 None；
- ``to_dict()`` 含新字段、不含 ``goal``。
"""

import pytest

from app.core.agents.agent_profile import (
    AgentProfile,
    AgentProfileToolSummary,
    default_developer_agent,
)
from app.core.agents.prompt_ref import PromptRef


def _make_profile(allowed_tools=None, prompt_ref=None, **overrides) -> AgentProfile:
    """构造用于测试的 AgentProfile 实例。

    参数:
        allowed_tools: 允许的工具清单；缺省为 ``["read_file", "write_file"]``。
        prompt_ref: 可选的 ``PromptRef`` 实例；缺省为 None。
        **overrides: 覆盖任意 ``AgentProfile`` 构造字段。

    返回:
        一个 ``AgentProfile`` 实例。

    异常:
        无。

    副作用:
        无。
    """

    base = {
        "agent_id": "test_agent",
        "role": "test-role",
        "description": "test description",
        "context_policy": "text_only_v1",
        "prompt_ref": prompt_ref,
    }
    base.update(overrides)
    # 默认工具清单，允许测试或 overrides 显式覆盖。
    if "allowed_tools" not in base:
        base["allowed_tools"] = (
            allowed_tools if allowed_tools is not None else ["read_file", "write_file"]
        )
    return AgentProfile(**base)


def test_agent_profile_has_no_goal_attribute():
    """断言 AgentProfile 实例不再拥有 ``goal`` 属性。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当实例仍暴露 ``goal`` 属性时由 pytest 抛出。

    副作用:
        无。
    """

    profile = _make_profile()
    assert not hasattr(profile, "goal")


def test_agent_profile_rejects_goal_keyword_on_construction():
    """断言构造时传入 ``goal=`` 会触发 TypeError（dataclass 无此字段）。

    参数:
        无。

    返回:
        无。

    异常:
        TypeError: 由 dataclass 因未知字段在构造时抛出。

    副作用:
        无。
    """

    with pytest.raises(TypeError):
        AgentProfile(
            agent_id="x",
            role="x",
            description="x",
            allowed_tools=[],
            context_policy="text_only_v1",
            goal="legacy goal",
        )


def test_to_tool_summary_excludes_runtime_fields():
    """断言 ``to_tool_summary()`` 仅包含约定的 7 个字段，且不含运行时内部字段。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当投影字段集合不符合约定，或误含 workflow/prompt_ref 时抛出。

    副作用:
        无。
    """

    profile = _make_profile(allowed_tools=["read_file", "write_file"])
    summary = profile.to_tool_summary()

    assert isinstance(summary, AgentProfileToolSummary)
    expected_fields = {
        "agent_id",
        "role",
        "description",
        "capabilities",
        "recommended_use_cases",
        "constraints",
        "tool_capability_summary",
    }
    assert set(summary.__dataclass_fields__.keys()) == expected_fields
    # 刻意排除运行时内部字段。
    for excluded in ("workflow", "context_policy", "turn", "runtime_event_loop", "prompt_ref"):
        assert excluded not in summary.__dataclass_fields__


def test_to_tool_summary_renders_allowed_tools():
    """断言 ``tool_capability_summary`` 由构造时传入的 ``allowed_tools`` 渲染。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当摘要未按约定形态渲染时由 pytest 抛出。

    副作用:
        无。
    """

    allowed_tools = ["read_file", "write_file"]
    profile = _make_profile(allowed_tools=allowed_tools)
    summary = profile.to_tool_summary()

    expected = "tools: " + ", ".join(allowed_tools)
    assert summary.tool_capability_summary == expected
    assert summary.tool_capability_summary.startswith("tools: ")
    assert "read_file" in summary.tool_capability_summary
    assert "write_file" in summary.tool_capability_summary
    # 摘要确实源自构造时传入的工具清单，而非硬编码。
    assert summary.tool_capability_summary.endswith("write_file")


def test_prompt_ref_defaults_to_none():
    """断言 ``prompt_ref`` 默认值为 None。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当默认值非 None 时由 pytest 抛出。

    副作用:
        无。
    """

    profile = _make_profile()
    assert profile.prompt_ref is None


def test_prompt_ref_stored_and_serialized():
    """断言传入 ``PromptRef`` 时正确存储，且 ``to_dict()`` 序列化为 dict。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当存储或序列化形态不符合预期时由 pytest 抛出。

    副作用:
        无。
    """

    ref = PromptRef(name="x", label="y")
    profile = _make_profile(prompt_ref=ref)
    assert profile.prompt_ref is ref

    data = profile.to_dict()
    assert isinstance(data["prompt_ref"], dict)
    assert data["prompt_ref"]["name"] == "x"
    assert data["prompt_ref"]["label"] == "y"


def test_default_parent_profile_capabilities_and_constraints_are_lists():
    """断言默认父 profile 的 ``capabilities``/``constraints`` 不为 None，而是 list。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当字段为 None 或类型不符时由 pytest 抛出。

    副作用:
        无。
    """

    parent = default_developer_agent()
    assert parent.capabilities is not None
    assert parent.constraints is not None
    assert isinstance(parent.capabilities, list)
    assert isinstance(parent.constraints, list)


def test_to_dict_contains_new_fields_and_excludes_goal():
    """断言 ``to_dict()`` 含新字段且不再含 ``goal``。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当字段集合不符合约定时由 pytest 抛出。

    副作用:
        无。
    """

    profile = _make_profile()
    data = profile.to_dict()

    for field_name in (
        "description",
        "delegation_type",
        "capabilities",
        "recommended_use_cases",
        "constraints",
        "prompt_ref",
    ):
        assert field_name in data

    assert "goal" not in data
