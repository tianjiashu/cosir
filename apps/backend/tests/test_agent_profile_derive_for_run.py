"""``AgentProfile.derive_for_run`` 的 per-run 派生契约单元测试。

单一职责：只验证该函数自身的四条契约——``ban_tools`` 按工具名收窄、共享单例不被原地写、
模型路由按 run 回填、``model_settings`` 显式覆盖。不覆盖 runner/workflow 链路（由各自的
集成测试负责）。

背景：``ban_tools`` 分支曾写成 ``changes["allowed_tools"] - ban_tools``，既读未初始化的
key（必抛 ``KeyError``）又把列表当集合做差集，导致所有传 ``ban_tools`` 的 child run 在进入
workflow 前就失败；本模块用于锁死该契约不再回退。
"""

from dataclasses import dataclass
from typing import Any

from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.model_settings import ModelSettings
from app.models.conversation_run_extra import ConversationRunExtra


@dataclass(frozen=True)
class _RunRoute:
    """Conversation Run 的最小替身：承载 per-run 派生需要的用户推理偏好。

    参数:
        extra: Run 用户运行偏好。

    返回:
        无（数据承载类型）。

    异常:
        无。

    副作用:
        无。
    """

    extra: ConversationRunExtra | None = None


def _child_profile() -> AgentProfile:
    """构造一个工具集已知的 CHILD profile，不依赖 agent 注册表。

    参数:
        无。

    返回:
        工具集为 ``read_file`` / ``write_file`` / ``delegate_task`` 的 ``AgentProfile``。

    异常:
        无。

    副作用:
        无（``workflow`` 传 ``None`` 以跳过默认 workfow 实例化，派生逻辑不消费该字段）。
    """

    return AgentProfile(
        agent_id="code-developer",
        role="developer",
        allowed_tools=["read_file", "write_file", "delegate_task"],
        agent_type=AgentProfileType.CHILD,
        system_prompt="Test child system prompt.",
        workflow=None,  # type: ignore[arg-type]
    )


def test_derive_for_run_ban_tools_narrows_allowed_tools() -> None:
    """``ban_tools`` 必须按工具名从 ``allowed_tools`` 中剔除，且不得抛异常。"""

    profile = _child_profile()

    derived = profile.derive_for_run(
        run=_RunRoute(),  # type: ignore[arg-type]
        ban_tools=["delegate_task"],
    )

    assert derived.allowed_tools == ["read_file", "write_file"]
    # 共享单例必须保持原样：派生只产出副本，不原地写。
    assert profile.allowed_tools == ["read_file", "write_file", "delegate_task"]


def test_derive_for_run_ignores_ban_names_not_in_allowed_tools() -> None:
    """``ban_tools`` 含有本 profile 未持有的工具名时必须静默忽略，而不是报错。"""

    profile = _child_profile()

    derived = profile.derive_for_run(
        run=_RunRoute(),  # type: ignore[arg-type]
        ban_tools=["not_a_tool", "terminal_start"],
    )

    assert derived.allowed_tools == profile.allowed_tools


def test_derive_for_run_without_ban_tools_keeps_allowed_tools() -> None:
    """未传 ``ban_tools`` 时工具白名单原样沿用。"""

    profile = _child_profile()

    derived = profile.derive_for_run(run=_RunRoute())  # type: ignore[arg-type]

    assert derived.allowed_tools == profile.allowed_tools


def test_derive_for_run_materializes_runtime_settings_and_applies_override() -> None:
    """per-run 已物化设置覆盖模型连接，profile 的用户覆盖仍然保留。"""

    profile = _child_profile()
    override = ModelSettings(temperature=0.3)

    runtime = ModelSettings(
        base_url="https://example.test",
        api_key="secret",
        model_name="run-model",
        context_window_k=128,
        supports_thinking=True,
        supports_reasoning_effort=True,
        supports_image=False,
    )
    derived = profile.derive_for_run(
        run=_RunRoute(),
        model_settings=runtime.with_overrides(override),
    )

    assert derived.model_settings.model_name == "run-model"
    assert derived.model_settings.api_key == "secret"
    assert derived.model_settings.temperature == 0.3
    # 原单例不被本次派生污染。
    assert not hasattr(profile, "model_config_id")
    assert profile.model_settings.temperature is None


def test_derive_for_run_run_reasoning_effort_overrides_profile() -> None:
    """Run 的推理强度是本次执行的最终用户偏好。"""

    profile = _child_profile()
    profile.model_settings = ModelSettings(reasoning_effort="high")

    derived = profile.derive_for_run(
        run=_RunRoute(
            extra=ConversationRunExtra(
                display_text="hello",
                attachments=[],
                reasoning_effort="low",
            )
        ),
    )

    assert derived.model_settings.reasoning_effort == "low"


def test_derive_for_run_binds_run_to_copy_only() -> None:
    """``run`` 只绑定在副本上，共享单例的 ``run`` 字段保持为空。"""

    profile = _child_profile()
    route: Any = _RunRoute()

    derived = profile.derive_for_run(run=route)

    assert derived.run is route
    assert profile.run is None
