"""FastAPI dependency wiring."""

import logging

from app.config.settings import default_settings
from app.core.agents.agent_profile import (
    default_developer_agent,
)
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.context import TextContextBuilder
from app.core.runtime.runner import AgentRuntime
from app.service.log_query_service import LogQueryService
from app.service.task.task_service import TaskService
from app.service.task.turn_service import TurnService
from app.service.task.workspace_service import WorkspaceService
from app.service.trace.trace_query_service import TraceQueryService
from app.storage.crud.log_crud import LogStore
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.trace_crud import TraceStore
from app.storage.crud.turn_crud import TurnCrud
from app.storage.crud.turn_message_crud import TurnMessageCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.store_engines import init_storage
from app.tools.tool_system import ToolSystem

_RUNTIME: AgentRuntime | None = None
_TOOL_SYSTEM: ToolSystem | None = None
_AGENT_REGISTRY: AgentProfileRegistry | None = None
_SERVICES: dict | None = None
_TRACE_QUERY_SERVICE_UNSET = object()
_TRACE_QUERY_SERVICE: TraceQueryService | None = _TRACE_QUERY_SERVICE_UNSET


def set_runtime(runtime: AgentRuntime) -> None:
    """Set the process-wide runtime instance.

    Parameters:
        runtime: Runtime instance to expose through dependency injection.

    Returns:
        None.

    Raises:
        None.

    Side effects:
        Replaces the module-level runtime singleton.
    """

    global _RUNTIME
    _RUNTIME = runtime


def set_tool_system(tool_system: ToolSystem) -> None:
    """Set the process-wide tool system instance.

    Parameters:
        tool_system: Initialized tool system built during application startup.

    Returns:
        None.

    Raises:
        None.

    Side effects:
        Replaces the module-level tool system singleton.
    """

    global _TOOL_SYSTEM
    _TOOL_SYSTEM = tool_system


def get_tool_system() -> ToolSystem:
    """Return the process-wide tool system instance.

    Parameters:
        None.

    Returns:
        Initialized ToolSystem.

    Raises:
        RuntimeError: If the tool system has not been initialized.

    Side effects:
        None.
    """

    if _TOOL_SYSTEM is None:
        raise RuntimeError("tool system has not been initialized")
    return _TOOL_SYSTEM


def get_runtime() -> AgentRuntime:
    """Return the process-wide runtime instance.

    Parameters:
        None.

    Returns:
        Configured runtime instance.

    Raises:
        RuntimeError: If the runtime has not been initialized.

    Side effects:
        None.
    """

    if _RUNTIME is None:
        raise RuntimeError("runtime has not been initialized")
    return _RUNTIME


def set_agent_registry(registry: AgentProfileRegistry) -> None:
    """Set the process-wide agent profile registry singleton.

    Parameters:
        registry: Initialized agent profile registry built during startup.

    Returns:
        None.

    Raises:
        None.

    Side effects:
        Replaces the module-level agent registry singleton.
    """

    global _AGENT_REGISTRY
    _AGENT_REGISTRY = registry


def get_agent_registry() -> AgentProfileRegistry:
    """Return the process-wide agent profile registry singleton.

    Parameters:
        None.

    Returns:
        Initialized AgentProfileRegistry.

    Raises:
        RuntimeError: If the agent registry has not been initialized.

    Side effects:
        None.
    """

    if _AGENT_REGISTRY is None:
        raise RuntimeError("agent registry has not been initialized")
    return _AGENT_REGISTRY


def build_agent_registry() -> AgentProfileRegistry:
    """构建并播种进程级 agent profile 目录。

    集中注册所有内置 agent；新增 agent 仅需在此多 ``register`` 一行。
    该函数是「启动时注册所有 agent」的单一落点，与 ``_RUNTIME`` / ``_TOOL_SYSTEM`` 同构。

    参数:
        无。

    返回:
        已播种完成的 ``AgentProfileRegistry``。

    异常:
        无。

    副作用:
        构造并填充进程级 registry 单例所依赖的 registry 实例。
    """

    registry = AgentProfileRegistry()
    registry.register(default_developer_agent())
    # registry.register(xxx_agent())  # 未来扩展点：新增内置 agent 仅多一行
    return registry


def build_runtime(
    tool_system: ToolSystem | None = None,
    settings=None,
) -> AgentRuntime:
    """Build the default runtime dependency graph.

    Parameters:
        tool_system: Initialized tool system supplied by application startup.
        settings: Optional backend settings. When omitted, default settings are
            loaded.
        logger: Optional application logger. When omitted, logging is installed
            from settings.

    Returns:
        Configured AgentRuntime instance.

    Raises:
        RuntimeError: If no tool system has been initialized or supplied, or if
            the agent registry has not been initialized.
        OSError: If logs or SQLite storage cannot be created.

    Side effects:
        Configures logging and initializes SQLite-backed stores.
    """

    settings = settings or default_settings()
    tool_system = tool_system or get_tool_system()
    services = _build_services(settings)
    # agent registry 由进程级单例提供（启动时经 set_agent_registry 注入），
    # 与 GET /agents 端点共享同一份目录。
    agent_registry = get_agent_registry()
    return AgentRuntime(
        settings=settings,
        task_service=services["task_service"],
        turn_service=services["turn_service"],
        context_builder=TextContextBuilder(),
        tool_scheduler=tool_system.scheduler,
        agent_registry=agent_registry,
        model_tools=tool_system.registry.get_all_definitions(),
    )


def _build_services(settings) -> dict:
    """Build and cache the process-wide domain service singletons.

    参数:
        settings: 后端运行配置。

    返回:
        含 ``task_service`` / ``turn_service`` / ``workspace_service`` 的字典。

    异常:
        无。

    副作用:
        首次调用时初始化 SQLite 存储并构建 service；结果在进程内缓存复用。
    """

    global _SERVICES
    if _SERVICES is not None:
        return _SERVICES
    init_storage(settings)
    task_crud = TaskCrud()
    turn_crud = TurnCrud()
    turn_message_crud = TurnMessageCrud()
    workspace_crud = WorkspaceCrud()
    _SERVICES = {
        "task_service": TaskService(task_crud, turn_crud, workspace_crud),
        "turn_service": TurnService(task_crud, turn_crud, turn_message_crud),
        "workspace_service": WorkspaceService(task_crud, turn_crud, workspace_crud),
    }
    return _SERVICES


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

    return _build_services(default_settings())["workspace_service"]


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

    return _build_services(default_settings())["task_service"]


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

    return _build_services(default_settings())["turn_service"]


def get_trace_query_service() -> TraceQueryService:
    """返回进程级 trace 查询 service 单例。

    参数:
        无。

    返回:
        TraceQueryService。

    异常:
        RuntimeError: 若 trace SQLite 不可用（例如首次构建失败，之后每次调用都抛）。

    副作用:
        首次调用时构建并缓存 service；失败则缓存 ``None`` 并抛 ``RuntimeError``。
    """

    global _TRACE_QUERY_SERVICE
    if _TRACE_QUERY_SERVICE is not _TRACE_QUERY_SERVICE_UNSET:
        if _TRACE_QUERY_SERVICE is None:
            raise RuntimeError("trace query service unavailable")
        return _TRACE_QUERY_SERVICE
    settings = default_settings()
    init_storage(settings)
    logger = logging.getLogger("coding_agent.backend")
    try:
        store = TraceStore()
    except Exception as exc:
        logger.warning(
            "trace_query_service_unavailable",
            extra={
                "msg": "trace SQLite is unavailable; trace query service disabled",
                "data": {"error": str(exc)},
            },
        )
        _TRACE_QUERY_SERVICE = None
        raise RuntimeError("trace query service unavailable") from exc
    _TRACE_QUERY_SERVICE = TraceQueryService(store, settings.log_dir)
    return _TRACE_QUERY_SERVICE


def _build_log_query_service(settings, logger):
    """Build the log query service when its SQLite store is available.

    Parameters:
        settings: Backend runtime settings.
        logger: Backend logger.

    Returns:
        LogQueryService or None.

    Raises:
        None.

    Side effects:
        Initializes the log SQLite store when possible; logs a warning on failure.
    """

    try:
        log_store = LogStore()
    except Exception as exc:
        logger.warning(
            "log_query_service_unavailable",
            extra={
                "msg": "log SQLite is unavailable; log query service disabled",
                "data": {"error": str(exc)},
            },
        )
        return None
    return LogQueryService(log_store, max_limit=settings.log_query_limit_max)
