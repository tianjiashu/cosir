"""进程级轻量配置单例的集中收口（轻量配置层的特例扩展）。

本模块属于 ``app.config`` 通用轻量层，但作为进程级配置类单例的单一收口点，
允许被 ``core`` / ``service`` / ``api`` / ``storage`` 各层安全导入，不存在反向依赖。

收口内容（进程级、按需构建、可热替换）：
- agent profile 目录（``AgentProfileRegistry``）：``set_agent_registry`` /
  ``get_agent_registry`` / ``build_agent_registry``。
- 工具系统（``ToolSystem``）：``set_tool_system`` / ``get_tool_system``。

设计要点：
- 两组单例均为 ``None`` 起步，由应用启动（``api/app.py`` 的 lifespan / ``create_app``）
  经对应 ``set_*`` 注入；未注入即 ``get_*`` 会抛出一致的 ``RuntimeError``，避免散落的
  模块级全局变量与 ``global`` 声明。
- 本模块仅收口轻量配置层可安全持有的 agent 目录与工具系统；运行时（``AgentRuntime``）
  单例（``set_runtime`` / ``get_runtime``）因依赖 service 装配与 ``RuntimeContextBuilder``，
  收口在 ``app.service.depends``；领域 service 与底层 CRUD/Store 单例同样由 ``app.service.depends``
  统一管理。本模块均不持有，避免 ``config`` 层耦合 service / core 装配。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.agents.agent_profile_registry import (
    AgentProfileRegistry,
)
from app.core.tools import ToolSystem
from app.core.tools.tool_registry import ToolRegistry

if TYPE_CHECKING:
    from app.core.agents.agent_profile import AgentProfile

_AGENT_REGISTRY: AgentProfileRegistry | None = None
_TOOL_SYSTEM: ToolSystem | None = None


def set_agent_registry(registry: AgentProfileRegistry) -> None:
    """设置进程级 agent profile 目录单例。

    参数:
        registry: 已播种完成的 agent profile 目录，由应用启动时注入。

    返回:
        无。

    异常:
        无。

    副作用:
        替换模块级 agent 目录单例；``get_agents`` / 执行引擎 / service 校验共享同一份。
    """

    global _AGENT_REGISTRY
    _AGENT_REGISTRY = registry


def get_agent_registry() -> AgentProfileRegistry:
    """返回进程级 agent profile 目录单例。

    参数:
        无。

    返回:
        已初始化的 ``AgentProfileRegistry``。

    异常:
        RuntimeError: 如果 agent 目录尚未初始化（未调用 ``set_agent_registry``）。

    副作用:
        无。
    """

    if _AGENT_REGISTRY is None:
        raise RuntimeError("agent registry has not been initialized")
    return _AGENT_REGISTRY


def build_agent_registry(
    main_agent_system_prompt: str = "",
    *,
    main_agent_max_steps: int = 300,
) -> AgentProfileRegistry:
    """构建只包含代码内置 profile 的进程级 Registry。

    内置 agent 构造器在本地延迟导入（``app.core.agents.define_agents`` 在模块级又会
    回引 ``app.config.configuration``，顶层导入会形成循环；只有本函数真正消费这些
    构造器，故在此处导入即可打破循环且不改变任何职责边界）。


    通用子 Agent 和主 Agent 由代码构造并注册到 `system` 作用域；主 Agent prompt 由系统配置
    service 从用户文件读取后注入，用户尚未配置时传空串（主 Agent 不生成 ``<agent_layer>``）。
    主 Agent 的 ``max_steps`` 由装配层传入，默认值与 ``Settings`` 的默认配置一致。
    系统与 workspace JSON 由生命周期启动阶段一次性读取到同一个 Registry。

    返回:
        已播种完成的 ``AgentProfileRegistry``。

    异常:
        AgentProfileConfigError: 代码内置 profile 存在重复 ID。

    副作用:
        构造并填充一个全新的 registry 实例（调用方持有，不写入进程级单例）。

    本函数是「启动时注册全局可用 Agent」的单一事实来源，供 ``api`` 层依赖注入与
    ``service`` 层（如 agent_id 合法性校验）复用，避免知识重复与跨层依赖。

    """

    from app.core.agents.define_agents import (
        general_child_agent,
        main_agent,
    )

    registry = AgentProfileRegistry()
    for profile in (
        general_child_agent(),
        main_agent(system_prompt=main_agent_system_prompt, max_steps=main_agent_max_steps),
    ):
        registry.register(AgentProfileRegistry.SYSTEM_WORKSPACE, profile)
    return registry


def replace_main_agent_profile(
    *,
    system_prompt: str | None = None,
    max_steps: int | None = None,
) -> AgentProfile:
    """按当前 Registry profile 重建并替换主 Agent。

    参数:
        system_prompt: 新的主 Agent prompt；为 ``None`` 时保留当前正文。
        max_steps: 新的单轮最大步数；为 ``None`` 时保留当前预算。

    返回:
        已替换进 Registry 的新主 Agent profile。

    异常:
        RuntimeError: Registry 未初始化，或其中不存在主 Agent profile。

    副作用:
        原子替换进程级 Registry 中的主 Agent profile。已经从旧 profile 派生的 Run 不受影响，
        后续从 Registry 解析 profile 的新 Run 使用新配置。
    """

    from app.core.agents.define_agents import main_agent

    registry = get_agent_registry()
    current = registry.resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, "main_agent")
    if current is None:
        raise RuntimeError("main agent profile is unavailable")
    replacement = main_agent(
        system_prompt=current.system_prompt if system_prompt is None else system_prompt,
        max_steps=current.max_steps if max_steps is None else max_steps,
    )
    registry.replace(AgentProfileRegistry.SYSTEM_WORKSPACE, replacement)
    return replacement


def set_tool_system(tool_system: ToolSystem) -> None:
    """设置进程级工具系统单例。

    参数:
        tool_system: 已初始化的工具系统，由应用启动时构建并注入。

    返回:
        无。

    异常:
        无。

    副作用:
        替换模块级工具系统单例。
    """

    global _TOOL_SYSTEM
    _TOOL_SYSTEM = tool_system


def get_tool_registry() -> ToolRegistry:
    """返回进程级工具注册表。

    参数:
        无。

    返回:
        已初始化的 ``ToolRegistry``。

    异常:
        RuntimeError: 工具系统尚未初始化（未调用 ``set_tool_system``）。

    副作用:
        无。
    """

    if _TOOL_SYSTEM is None:
        raise RuntimeError("tool system has not been initialized")
    return _TOOL_SYSTEM.registry


def get_tool_system() -> ToolSystem:
    """返回进程级工具系统单例。

    参数:
        无。

    返回:
        已初始化的 ``ToolSystem``。

    异常:
        RuntimeError: 如果工具系统尚未初始化（未调用 ``set_tool_system``）。

    副作用:
        无。
    """

    if _TOOL_SYSTEM is None:
        raise RuntimeError("tool system has not been initialized")
    return _TOOL_SYSTEM
