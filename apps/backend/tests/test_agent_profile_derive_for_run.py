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


@dataclass(frozen=True)
class _RunRoute:
    """Conversation Run 的最小替身：只承载 ``derive_for_run`` 读取的两个路由字段。

    参数:
        provider_id: 模型厂商标识。
        model_name: 模型名称。

    返回:
        无（数据承载类型）。

    异常:
        无。

    副作用:
        无。
    """

    provider_id: int | None = None
    model_name: str | None = None


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


def test_derive_for_run_backfills_route_and_applies_model_settings_override() -> None:
    """未配置路由的 profile 从 run 回填 provider/model，显式覆盖模型参数生效。"""

    profile = _child_profile()
    override = ModelSettings(temperature=0.3)

    derived = profile.derive_for_run(
        run=_RunRoute(provider_id=3, model_name="glm-4.6"),  # type: ignore[arg-type]
        model_settings=override,
    )

    assert (derived.provider_id, derived.model_name) == (3, "glm-4.6")
    assert derived.model_settings is override
    # 原单例不被本次派生污染。
    assert profile.provider_id is None
    assert profile.model_name is None
    assert profile.model_settings != override


def test_derive_for_run_keeps_profile_explicit_route() -> None:
    """profile 已显式配置的模型路由优先于 run，不被回填覆盖。"""

    profile = _child_profile()
    profile.provider_id = 9
    profile.model_name = "own-model"

    derived = profile.derive_for_run(
        run=_RunRoute(provider_id=3, model_name="glm-4.6"),  # type: ignore[arg-type]
    )

    assert (derived.provider_id, derived.model_name) == (9, "own-model")


def test_derive_for_run_binds_run_to_copy_only() -> None:
    """``run`` 只绑定在副本上，共享单例的 ``run`` 字段保持为空。"""

    profile = _child_profile()
    route: Any = _RunRoute(provider_id=1, model_name="m")

    derived = profile.derive_for_run(run=route)

    assert derived.run is route
    assert profile.run is None
