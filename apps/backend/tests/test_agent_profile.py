"""AgentProfile 作为能力事实源的单元测试。

覆盖的核心契约：
- ``goal`` 字段已移除；
- 所有 agent 描述收敛到 ``description``（不再有 ``capabilities``/``recommended_use_cases``/
  ``constraints``/``delegation_type`` 等分散字段）；
- ``to_dict()`` 仅暴露稳定字段，字段集与 ``AgentProfileResponse`` 对齐、不含 ``goal``；
- ``prompt_ref`` 默认 None，传入时正确存储并序列化。
"""

import pytest

from app.core.agents.agent_profile import AgentProfile
from app.core.agents.define_agents import default_developer_agent
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


def test_agent_profile_excludes_split_description_fields():
    """断言 AgentProfile 不再暴露拆分描述字段。

    所有 agent 描述已收敛到 ``description``，``capabilities``/``recommended_use_cases``/
    ``constraints``/``delegation_type`` 字段不应存在。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当仍暴露任一拆分字段时由 pytest 抛出。

    副作用:
        无。
    """

    profile = _make_profile()
    for removed in ("capabilities", "recommended_use_cases", "constraints", "delegation_type"):
        assert not hasattr(profile, removed)


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


def test_to_dict_excludes_split_fields_and_goal():
    """断言 ``to_dict()`` 不含拆分描述字段与 ``goal``，且字段集与响应模型对齐。

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

    expected_fields = {
        "agent_id",
        "role",
        "description",
        "allowed_tools",
        "context_policy",
        "workflow",
        "model_name",
        "max_steps",
        "prompt_ref",
    }
    assert set(data.keys()) == expected_fields

    for excluded in (
        "goal",
        "delegation_type",
        "capabilities",
        "recommended_use_cases",
        "constraints",
    ):
        assert excluded not in data


def test_default_parent_profile_description_is_nonempty():
    """断言默认父 profile 的 ``description`` 非空且为收敛后的单一描述字段。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当 description 为空或仍存在拆分字段时由 pytest 抛出。

    副作用:
        无。
    """

    parent = default_developer_agent()
    assert parent.description
    for removed in ("capabilities", "recommended_use_cases", "constraints", "delegation_type"):
        assert not hasattr(parent, removed)
