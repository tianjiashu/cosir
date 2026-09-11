"""Tests for delegate_task model configuration inheritance."""

from types import SimpleNamespace

from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.model_settings import ModelSettings
from app.core.delegation.delegation_executor import DelegationExecutor


def _profile(*, provider_id: int | None = None, model_name: str | None = None) -> AgentProfile:
    """构造不依赖工具注册表的最小 child profile。"""

    return AgentProfile(
        agent_id="child",
        role="child",
        allowed_tools=["read_file"],
        agent_type=AgentProfileType.CHILD,
        provider_id=provider_id,
        model_name=model_name,
    )


def _executor_with_parent_run() -> DelegationExecutor:
    """构造仅用于测试模型配置解析的 executor。"""

    executor = object.__new__(DelegationExecutor)
    executor._parent_run = SimpleNamespace(  # type: ignore[attr-defined]
        provider_id=7,
        model_name="parent-model",
        reasoning_effort="high",
    )
    return executor


def test_child_model_config_inherits_parent_run_by_default() -> None:
    """内置 child 未配置模型时必须完整继承父 Run 路由和推理强度。"""

    executor = _executor_with_parent_run()

    assert executor._resolve_child_model_config(_profile()) == (7, "parent-model", "high")


def test_child_model_route_can_override_parent_run() -> None:
    """未来 child profile 显式配置 provider/model 时仍保留自定义入口。"""

    executor = _executor_with_parent_run()

    assert executor._resolve_child_model_config(
        _profile(provider_id=9, model_name="child-model")
    ) == (9, "child-model", "high")


def test_child_reasoning_effort_can_override_parent_run() -> None:
    """未来 child profile 显式配置推理强度时仍保留自定义入口。"""

    executor = _executor_with_parent_run()
    child = _profile()
    child.model_settings = ModelSettings(reasoning_effort="low")

    assert executor._resolve_child_model_config(child) == (7, "parent-model", "low")
