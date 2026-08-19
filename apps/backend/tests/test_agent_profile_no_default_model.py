"""阶段 1.5：5 个内置 Agent profile 不内置默认模型策略测试。

覆盖目标（设计文档阶段 1.5 验收）：
- 5 个内置 profile（``developer`` / ``delegate_reviewer`` / ``delegate_analyst``
  / ``delegate_tester`` / ``delegate_coder``）的 ``model_name`` 字段均为 ``None``；
- ``AgentProfile.to_dict`` 在 ``model_name=None`` 时不抛异常，可序列化为 JSON；
- ``AgentProfile`` 不传 ``model_name`` 时默认 ``None``（构造契约）；
- ``ErrorKind.MODEL_NOT_CONFIGURED`` 枚举值稳定为 ``"model_not_configured"``；
- ``RunFailedPayload`` 新增的 ``error_code`` / ``guidance`` 字段默认为 ``None``
  且可被显式赋值（设计文档阶段 1.5 引入）。
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.core.agents.agent_profile import AgentProfile
from app.core.agents.define_agents import (
    analyst_agent,
    coder_agent,
    default_developer_agent,
    developer_agent,
    reviewer_agent,
)
from app.core.agents.define_agents import test_agent as build_tester_agent
from app.models.enums.error_kind import ErrorKind
from app.models.payload.run_failed_payload import RunFailedPayload


def test_developer_agent_has_no_default_model() -> None:
    """``developer`` profile 不内置默认模型（``model_name is None``）。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当 ``developer_agent().model_name`` 非 None 时由 pytest 抛出。

    副作用:
        无。
    """

    profile = developer_agent()
    assert (
        profile.model_name is None
    ), "developer profile must not hardcode a default model after stage 1.5"


def test_default_developer_agent_alias_has_no_default_model() -> None:
    """``default_developer_agent`` 别名与 ``developer_agent`` 一致（``model_name is None``）。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当别名返回的 profile ``model_name`` 非 None 时由 pytest 抛出。

    副作用:
        无。
    """

    profile = default_developer_agent()
    assert profile.model_name is None


@pytest.mark.parametrize(
    "builder",
    [
        reviewer_agent,
        analyst_agent,
        build_tester_agent,
        coder_agent,
    ],
    ids=["reviewer", "analyst", "tester", "coder"],
)
def test_delegate_profiles_have_no_default_model(builder: Callable[[], AgentProfile]) -> None:
    """4 个委派子 profile 均不内置默认模型（``model_name is None``）。

    参数:
        builder: 构造子 profile 的工厂函数。

    返回:
        无。

    异常:
        AssertionError: 当任一子 profile ``model_name`` 非 None 时由 pytest 抛出。

    副作用:
        无。
    """

    profile = builder()
    assert profile.model_name is None, (
        f"delegate profile {profile.agent_id} must not hardcode a default model "
        f"after stage 1.5 (got {profile.model_name!r})"
    )


def test_agent_profile_default_model_name_is_none_when_not_passed() -> None:
    """``AgentProfile`` 不传 ``model_name`` 时默认 ``None``（构造契约）。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当默认值非 None 时由 pytest 抛出。

    副作用:
        无。
    """

    profile = AgentProfile(
        agent_id="adhoc",
        role="adhoc-role",
        description="adhoc description",
        allowed_tools=["read_file"],
    )
    assert profile.model_name is None


def test_agent_profile_to_dict_serializes_none_model_name() -> None:
    """``to_dict`` 在 ``model_name=None`` 时不抛异常（前端可区分未配置）。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当 ``to_dict`` 抛异常或 ``model_name`` 字段非 None 时由 pytest 抛出。

    副作用:
        无。
    """

    profile = AgentProfile(
        agent_id="adhoc",
        role="adhoc-role",
        description="adhoc description",
        allowed_tools=["read_file"],
    )
    serialized = profile.to_dict()
    assert serialized["model_name"] is None


def test_error_kind_model_not_configured_value_is_stable() -> None:
    """``ErrorKind.MODEL_NOT_CONFIGURED`` 枚举值稳定为 ``"model_not_configured"``。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当枚举字符串值偏离设计文档约定时由 pytest 抛出。

    副作用:
        无。
    """

    assert ErrorKind.MODEL_NOT_CONFIGURED.value == "model_not_configured"
    assert str(ErrorKind.MODEL_NOT_CONFIGURED) == "model_not_configured"


def test_run_failed_payload_new_fields_default_none() -> None:
    """``RunFailedPayload`` 新增 ``error_code`` / ``guidance`` 字段默认 ``None``。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当字段默认值非 None 或字段不存在时由 pytest 抛出。

    副作用:
        无。
    """

    payload = RunFailedPayload(error="something went wrong")
    assert payload.error_code is None
    assert payload.guidance is None


def test_run_failed_payload_supports_explicit_error_code_and_guidance() -> None:
    """``RunFailedPayload`` 可显式赋值 ``error_code`` / ``guidance``。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当显式赋值的字段未被原样保留时由 pytest 抛出。

    副作用:
        无。
    """

    payload = RunFailedPayload(
        error="model not configured",
        error_code=ErrorKind.MODEL_NOT_CONFIGURED.value,
        guidance="请先在对话窗口选择模型后再发送消息",
    )
    assert payload.error_code == "model_not_configured"
    assert payload.guidance.startswith("请先在对话窗口")
