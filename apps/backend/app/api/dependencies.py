"""FastAPI dependency wiring.

轻量配置单例（``AgentProfileRegistry`` / ``ToolSystem``）已收口到 ``app.config.configuration``，
由本模块做薄壳 re-export，使现有 ``Depends(get_agent_registry)`` / ``app.py`` 等调用点零改动；
运行时单例（``AgentRuntime``）因依赖 service 装配与 ``RuntimeContextBuilder``，由本模块自身
持有（``set_runtime`` / ``get_runtime`` / ``build_runtime``）。领域 service 与底层 CRUD/Store
单例由 ``app.service.depends`` 统一管理。
"""

from app.config.configuration import (
    build_agent_registry,
    get_agent_registry,
    get_tool_system,
    set_agent_registry,
    set_tool_system,
)
from app.core.runtime.runner import AgentRuntime
from app.service import depends as service_depends
from app.service.agent_runtime_event.runtime_event_bus import RuntimeEventBus
from app.service.agent_runtime_event.runtime_event_service import RuntimeEventService
from app.llm_provider.model_resolver_service import ModelResolverService
from app.llm_provider.provider import ModelEntryService
from app.llm_provider.provider.provider_connection_test_service import ProviderConnectionTestService
from app.llm_provider.provider import ProviderDiscoverService
from app.llm_provider.provider import ProviderService
from app.service.log_query_service import LogQueryService
from app.service.task.task_service import TaskService
from app.service.task.turn_service import TurnService
from app.service.task.turn_stream_service import TurnStreamService
from app.service.task.turn_workspace_resolver import TurnWorkspaceResolver
from app.service.task.workspace_service import WorkspaceService
from app.service.workspace_event.workspace_event_bus import WorkspaceEventBus
from app.service.workspace_event.workspace_event_service import WorkspaceEventService

# 已迁移到 ``app.config.configuration`` 的进程级单例访问器，在此 re-export 以保持
# ``Depends(get_agent_registry)`` / ``app.py`` 等既有调用点零改动。
__all__ = [
    "build_agent_registry",
    "get_agent_registry",
    "get_model_entry_service",
    "get_model_resolver_service",
    "get_provider_connection_test_service",
    "get_provider_discover_service",
    "get_provider_service",
    "get_tool_system",
    "set_agent_registry",
    "set_tool_system",
]

_RUNTIME: "AgentRuntime | None" = None


def _build_services() -> dict:
    """Build the process-wide domain service mapping.

    参数:
        无。后端运行配置由 ``Settings`` 类级静态属性提供，不以对象传入。

    返回:
        含 ``runtime_event_bus`` / ``runtime_event_service`` / ``task_service`` /
        ``turn_service`` / ``turn_stream_service`` / ``workspace_service`` /
        ``log_query_service`` 的字典。

    异常:
        RuntimeError: 如果应用启动尚未初始化 storage。

    副作用:
        具体 service 单例由 ``app.service.depends`` 缓存复用。
    """

    return {
        "runtime_event_bus": service_depends.get_runtime_event_bus(),
        "runtime_event_service": service_depends.get_runtime_event_service(),
        "task_service": service_depends.get_task_service(),
        "turn_service": service_depends.get_turn_service(),
        "turn_stream_service": service_depends.get_turn_stream_service(),
        "workspace_service": service_depends.get_workspace_service(),
        "log_query_service": service_depends.get_log_query_service(),
    }


def get_workspace_service() -> WorkspaceService:
    """返回进程级工作区 service 单例。

    参数:
        无。

    返回:
        WorkspaceService。

    异常:
        RuntimeError: 若存储初始化失败。

    副作用:
        首次调用时构建并缓存 service。
    """

    return _build_services()["workspace_service"]


def get_task_service() -> TaskService:
    """返回进程级任务 service 单例。

    参数:
        无。

    返回:
        TaskService。

    异常:
        RuntimeError: 若存储初始化失败。

    副作用:
        首次调用时构建并缓存 service。
    """

    return _build_services()["task_service"]


def get_turn_service() -> TurnService:
    """返回进程级轮次 service 单例。

    参数:
        无。

    返回:
        TurnService。

    异常:
        RuntimeError: 若存储初始化失败。

    副作用:
        首次调用时构建并缓存 service。
    """

    return _build_services()["turn_service"]


def get_turn_stream_service() -> TurnStreamService:
    """返回进程级轮次事件流编排 service 单例。

    参数:
        无。

    返回:
        TurnStreamService。

    异常:
        RuntimeError: 若存储初始化失败。

    副作用:
        首次调用时构建并缓存 service。
    """

    return _build_services()["turn_stream_service"]


def get_turn_workspace_resolver() -> TurnWorkspaceResolver:
    """返回进程级 turn → workspace 解析器单例。

    参数:
        无。

    返回:
        TurnWorkspaceResolver。

    异常:
        RuntimeError: 若存储初始化失败。

    副作用:
        首次调用时构建并缓存 service。
    """

    return service_depends.get_turn_workspace_resolver()


def get_workspace_event_bus() -> WorkspaceEventBus:
    """返回进程级 workspace 状态事件总线单例。

    参数:
        无。

    返回:
        WorkspaceEventBus 单例。

    异常:
        无。

    副作用:
        首次调用时构建并缓存总线。
    """

    return service_depends.get_workspace_event_bus()


def get_workspace_event_service() -> WorkspaceEventService | None:
    """返回进程级 workspace 状态事件 service；CodeGraph 不可用时返回 None。

    参数:
        无。

    返回:
        WorkspaceEventService 实例；CodeGraph Kernel 不可用时返回 None（降级到文件搜索）。

    异常:
        RuntimeError: 若存储初始化失败。

    副作用:
        尝试从 supervisor 取得 Kernel client。
    """

    return service_depends.get_workspace_event_service()


def get_log_query_service() -> LogQueryService:
    """返回进程级日志查询 service 单例。

    参数:
        无。

    返回:
        LogQueryService。

    异常:
        RuntimeError: 若存储初始化失败。

    副作用:
        首次调用时构建并缓存 service。
    """

    return _build_services()["log_query_service"]


def get_runtime_event_bus() -> RuntimeEventBus:
    """返回进程级 runtime event 广播总线。

    参数:
        无。

    返回:
        RuntimeEventBus。

    异常:
        RuntimeError: 若存储初始化失败。

    副作用:
        首次调用时构建并缓存 service。
    """

    return _build_services()["runtime_event_bus"]


def get_runtime_event_service() -> RuntimeEventService:
    """返回进程级 runtime event 持久化与广播 service。

    参数:
        无。

    返回:
        RuntimeEventService。

    异常:
        RuntimeError: 若存储初始化失败。

    副作用:
        首次调用时构建并缓存 service。
    """

    return _build_services()["runtime_event_service"]


def get_provider_service() -> ProviderService:
    """返回进程级厂商 service 单例（薄壳转发到 ``app.service.depends``）。

    参数:
        无。

    返回:
        ProviderService。

    异常:
        RuntimeError: 若存储初始化失败。

    副作用:
        首次调用时构建并缓存 service。
    """

    return service_depends.get_provider_service()


def get_provider_discover_service() -> ProviderDiscoverService:
    """返回进程级厂商发现 service 单例（薄壳转发到 ``app.service.depends``）。

    参数:
        无。

    返回:
        ProviderDiscoverService。

    异常:
        RuntimeError: 若存储初始化失败。

    副作用:
        首次调用时构建并缓存 service。
    """

    return service_depends.get_provider_discover_service()


def get_model_entry_service() -> ModelEntryService:
    """返回进程级模型条目 service 单例（薄壳转发到 ``app.service.depends``）。

    参数:
        无。

    返回:
        ModelEntryService。

    异常:
        RuntimeError: 若存储初始化失败。

    副作用:
        首次调用时构建并缓存 service。
    """

    return service_depends.get_model_entry_service()


def get_model_resolver_service() -> ModelResolverService:
    """返回进程级模型解析 service 单例（薄壳转发到 ``app.service.depends``）。

    参数:
        无。

    返回:
        ModelResolverService。

    异常:
        RuntimeError: 若存储初始化失败。

    副作用:
        首次调用时构建并缓存 service。
    """

    return service_depends.get_model_resolver_service()


def get_provider_connection_test_service() -> ProviderConnectionTestService:
    """返回进程级厂商连通性测试 service 单例（薄壳转发到 ``app.service.depends``）。

    参数:
        无。

    返回:
        ProviderConnectionTestService。

    异常:
        RuntimeError: 若存储初始化失败。

    副作用:
        首次调用时构建并缓存 service。
    """

    return service_depends.get_provider_connection_test_service()


def set_runtime(runtime: "AgentRuntime") -> None:
    """设置进程级运行时单例。

    参数:
        runtime: 已构建的运行时实例，由应用启动时经 ``build_runtime`` 产出并注入。

    返回:
        无。

    异常:
        无。

    副作用:
        替换模块级运行时单例。
    """

    global _RUNTIME
    _RUNTIME = runtime


def get_runtime() -> "AgentRuntime":
    """返回进程级运行时单例。

    参数:
        无。

    返回:
        已配置的 ``AgentRuntime`` 实例。

    异常:
        RuntimeError: 如果运行时尚未初始化（未调用 ``set_runtime``）。

    副作用:
        无。
    """

    if _RUNTIME is None:
        raise RuntimeError("runtime has not been initialized")
    return _RUNTIME
