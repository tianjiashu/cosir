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
  单例因依赖 service 装配与 ``RuntimeContextBuilder``，收口在 ``api/depends/dependencies.py``
  （``set_runtime`` / ``get_runtime`` / ``build_runtime``）；领域 service 与底层 CRUD/Store
  单例由 ``app.service.depends`` 统一管理。本模块均不持有，避免 ``config`` 层耦合
  service / core 装配。
"""

from app.core.agents.agent_profile import default_developer_agent, developer_agent_pro
from app.core.agents.agent_profile_registry import (
    AgentProfileRegistry,
)
from app.tools.tool_system import ToolSystem

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


def build_agent_registry() -> AgentProfileRegistry:
    """构建并播种默认的内置 agent profile 目录。

    集中注册所有内置 agent；新增 agent 仅需在此多 ``register`` 一行。
    本函数是「启动时注册所有 agent」的单一事实来源，供 ``api`` 层依赖注入与
    ``service`` 层（如 agent_id 合法性校验）复用，避免知识重复与跨层依赖。

    参数:
        无。

    返回:
        已播种完成的 ``AgentProfileRegistry``。

    异常:
        无。

    副作用:
        构造并填充一个全新的 registry 实例（调用方持有，不写入进程级单例）。
    """

    registry = AgentProfileRegistry()
    registry.register(default_developer_agent())
    registry.register(developer_agent_pro())
    # registry.register(xxx_agent())  # 未来扩展点：新增内置 agent 仅多一行
    return registry


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
