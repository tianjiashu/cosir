"""AgentProfile 作为能力事实源的单元测试。

覆盖的核心契约：
- ``goal`` 字段已移除；
- 所有 agent 描述收敛到 ``description``（不再有 ``capabilities``/``recommended_use_cases``/
  ``constraints``/``delegation_type`` 等分散字段）；
- ``to_dict()`` 仅暴露稳定字段，字段集与 ``AgentProfileResponse`` 对齐、不含 ``goal``；
- ``prompt_ref`` 默认 None，传入时正确存储并序列化。
"""

import asyncio
from datetime import UTC, datetime

import pytest

from app.core.agents.agent_profile import AgentProfile
from app.core.agents.define_agents import default_developer_agent
from app.core.agents.prompt_ref import PromptRef
from app.models.turn_record import TurnRecord


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


def _make_turn(turn_id: str) -> TurnRecord:
    """构造最小可用的 TurnRecord 测试实例。

    参数:
        turn_id: 轮次 ID。

    返回:
        一个 pending 状态的 ``TurnRecord``。

    异常:
        无。

    副作用:
        无。
    """

    now = datetime.now(UTC)
    return TurnRecord(
        turn_id=turn_id,
        task_id="task-1",
        input_text="hello",
        status="pending",
        created_at=now,
        updated_at=now,
    )


def test_derive_for_turn_returns_independent_copy():
    """断言 ``derive_for_turn`` 返回独立副本且不修改原实例。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当副本复用原实例或原实例被原地改写时由 pytest 抛出。

    副作用:
        无。
    """

    loop = asyncio.new_event_loop()
    try:
        profile = _make_profile()
        turn = _make_turn("t-1")
        derived = profile.derive_for_turn(
            turn,
            allowed_tools=["read_file"],
            runtime_event_loop=loop,
        )

        # 独立实例 + 原实例未被改写
        assert derived is not profile
        assert profile.turn is None
        assert profile.allowed_tools == ["read_file", "write_file"]
        assert profile.runtime_event_loop is None

        # 副本绑定本次执行字段
        assert derived.turn is turn
        assert derived.allowed_tools == ["read_file"]
        assert derived.runtime_event_loop is loop

        # 静态配置沿用
        assert derived.agent_id == profile.agent_id
        assert derived.role == profile.role
        assert derived.description == profile.description
    finally:
        loop.close()


def test_derive_for_turn_omits_optional_overrides_by_default():
    """断言不传可选覆盖参数时副本沿用当前值。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当默认覆盖行为不符合预期时由 pytest 抛出。

    副作用:
        无。
    """

    profile = _make_profile()
    turn = _make_turn("t-2")
    derived = profile.derive_for_turn(turn)

    assert derived.turn is turn
    assert derived.allowed_tools is profile.allowed_tools
    assert derived.runtime_event_loop is None
    assert derived.agent_id == profile.agent_id
