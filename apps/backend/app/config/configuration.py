"""进程级轻量配置单例的集中收口（轻量配置层的特例扩展）。

本模块属于 ``app.config`` 通用轻量层，但作为进程级配置类单例的单一收口点，
允许被 ``core`` / ``service`` / ``api`` / ``storage`` 各层安全导入，不存在反向依赖。

收口内容（进程级、按需构建、可热替换）：
- agent profile 目录（``AgentProfileRegistry``）：``set_agent_registry`` /
  ``get_agent_registry`` / ``build_agent_registry``。
- 工具注册表与执行器：``set_tool_registry`` / ``get_tool_registry`` / ``get_tool_executor``。

设计要点：
- 两组单例均为 ``None`` 起步，由应用启动（``api/app.py`` 的 lifespan / ``create_app``）
  经对应 ``set_*`` 注入；未注入即 ``get_*`` 会抛出一致的 ``RuntimeError``，避免散落的
  模块级全局变量与 ``global`` 声明。
- 本模块仅收口轻量配置层可安全持有的 agent 目录与工具注册表、执行器；领域 service 与底层 CRUD/Store
  单例由 ``app.service.depends`` 统一管理。本模块均不持有，避免 ``config`` 层耦合
  service / core 装配。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.agents.agent_profile_registry import (
    AgentProfileRegistry,
)
from app.core.tool.tool_registry import ToolRegistry

if TYPE_CHECKING:
    from app.core.tool.execute.tool_executor import ToolExecutor

_AGENT_REGISTRY: AgentProfileRegistry | None = None
_TOOL_REGISTRY: ToolRegistry | None = None
_TOOL_EXECUTOR: ToolExecutor | None = None


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
    """构建进程级 Registry，并补齐内置子 Agent 的配置文件。

    Registry 不在这里装载任何 profile：主 Agent 与子 Agent 都在作用域被读取时由
    ``AgentProfileScopeSource`` 从配置文件生成，签名变了就重读（见 ``AgentProfileRegistry``）。内置子
    Agent 的文件在这里补齐，写入目录取自同一个来源映射，保证读取时读得到。

    构造器在本地延迟导入：配置 service 在模块级又会回引 ``app.config.configuration``，顶层导入会形成循环。

    副作用:
        可能在系统 ``.cosir/agents`` 下写入缺失的内置子 Agent 文件（失败只记日志）；构造一个全新的
        registry 实例（调用方持有，不写入进程级单例）。
    """

    from app.core.agents.agent_profile_source import AgentProfileScopeSource
    from app.service.configuration.agents.builtin_agents import seed_builtin_agent_files

    registry = AgentProfileRegistry(AgentProfileScopeSource())
    seed_builtin_agent_files(registry.source.directory(AgentProfileRegistry.SYSTEM_WORKSPACE))
    return registry


def set_tool_registry(registry: ToolRegistry) -> None:
    """注入进程级工具注册表，并在同一个注册表上装配执行器。

    两个单例由这一处一起设置，执行器因此不可能持有与 ``get_tool_registry`` 不同的注册表。执行器
    延迟导入：执行管线经 workflow 依赖本模块，模块级导入会形成循环。
    """

    from app.core.tool.execute.tool_executor import ToolExecutor

    global _TOOL_REGISTRY, _TOOL_EXECUTOR
    _TOOL_REGISTRY = registry
    _TOOL_EXECUTOR = ToolExecutor(registry=registry)


def get_tool_registry() -> ToolRegistry:
    """返回进程级工具注册表；未调用 ``set_tool_registry`` 时抛 ``RuntimeError``。"""

    if _TOOL_REGISTRY is None:
        raise RuntimeError("tool registry has not been initialized")
    return _TOOL_REGISTRY


def get_tool_executor() -> ToolExecutor:
    """返回进程级工具执行器；未调用 ``set_tool_registry`` 时抛 ``RuntimeError``。"""

    if _TOOL_EXECUTOR is None:
        raise RuntimeError("tool registry has not been initialized")
    return _TOOL_EXECUTOR
