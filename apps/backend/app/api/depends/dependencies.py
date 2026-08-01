"""FastAPI dependency wiring.

进程级运行时单例（``AgentProfileRegistry`` / ``ToolSystem`` / ``AgentRuntime``）已收口到
``app.config.configuration``；本模块仅保留领域 service 三件套单例（``_SERVICES``）及
``build_runtime`` 装配，并对 ``configuration`` 中的单例访问器做薄壳 re-export，使现有
``Depends(get_agent_registry)`` / ``app.py`` 等调用点零改动。
"""

from app.config.configuration import (
    build_agent_registry,
    get_agent_registry,
    get_tool_system,
    set_agent_registry,
    set_tool_system,
)
from app.core.context import RuntimeContextBuilder
from app.core.runtime.runner import AgentRuntime
from app.service.runtime_event.runtime_event_bus import RuntimeEventBus
from app.service.runtime_event.runtime_event_service import RuntimeEventService
from app.service.task.task_service import TaskService
from app.service.task.turn_service import TurnService
from app.service.task.workspace_service import WorkspaceService
from app.storage.crud.runtime_event_crud import RuntimeEventCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.turn_crud import TurnCrud
from app.storage.crud.turn_message_crud import TurnMessageCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.store_engines import init_storage
from app.tools.tool_system import ToolSystem

# 已迁移到 ``app.config.configuration`` 的进程级单例访问器，在此 re-export 以保持
# ``Depends(get_agent_registry)`` / ``app.py`` 等既有调用点零改动。
__all__ = [
    "build_agent_registry",
    "get_agent_registry",
    "get_tool_system",
    "set_agent_registry",
    "set_tool_system",
]

_SERVICES: dict | None = None
_RUNTIME: "AgentRuntime | None" = None


def build_runtime(
    tool_system: ToolSystem | None = None,
) -> AgentRuntime:
    """Build the default runtime dependency graph.

    Parameters:
        tool_system: Initialized tool system supplied by application startup.

    Returns:
        Configured AgentRuntime instance.

    Raises:
        RuntimeError: If no tool system has been initialized or supplied, or if
            the agent registry has not been initialized.
        OSError: If logs or SQLite storage cannot be created.

    Side effects:
        Configures logging and initializes SQLite-backed stores. Backend runtime
        limits are read from module-level static configuration instead of a
        passed-in settings object.
    """

    tool_system = tool_system or get_tool_system()
    services = _build_services()
    # agent registry 由进程级单例提供（启动时经 set_agent_registry 注入），
    # 与 GET /agents 端点共享同一份目录。
    agent_registry = get_agent_registry()
    return AgentRuntime(
        task_service=services["task_service"],
        turn_service=services["turn_service"],
        context_builder=RuntimeContextBuilder(),
        tool_scheduler=tool_system.scheduler,
        agent_registry=agent_registry,
        workspace_service=services["workspace_service"],
        runtime_event_service=services["runtime_event_service"],
    )


def _build_services() -> dict:
    """Build and cache the process-wide domain service singletons.

    参数:
        无。后端运行配置由 ``Settings`` 类级静态属性提供，不以对象传入。

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
    init_storage()
    task_crud = TaskCrud()
    turn_crud = TurnCrud()
    turn_message_crud = TurnMessageCrud()
    workspace_crud = WorkspaceCrud()
    runtime_event_crud = RuntimeEventCrud()
    runtime_event_bus = RuntimeEventBus()
    runtime_event_service = RuntimeEventService(runtime_event_crud, runtime_event_bus)
    _SERVICES = {
        "runtime_event_bus": runtime_event_bus,
        "runtime_event_service": runtime_event_service,
        "task_service": TaskService(
            task_crud, turn_crud, workspace_crud, runtime_event_crud, turn_message_crud
        ),
        "turn_service": TurnService(
            task_crud,
            turn_crud,
            turn_message_crud,
        ),
        "workspace_service": WorkspaceService(
            task_crud, turn_crud, workspace_crud, runtime_event_crud, turn_message_crud
        ),
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


def get_runtime_event_crud() -> RuntimeEventCrud:
    """返回运行时事件 CRUD 实例（用于事件回放查询）。

    事件回放属于只读历史重建，仅依赖 ``runtime_events`` 表；``RuntimeEventCrud``
    为无状态封装，每次调用实例化避免跨请求复用 session（与 ``TaskCrud`` 等同构）。

    参数:
        无。

    返回:
        RuntimeEventCrud。

    异常:
        RuntimeError: 若存储初始化失败。

    副作用:
        无。
    """

    return RuntimeEventCrud()


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
