"""FastAPI dependency wiring.

轻量配置单例（``AgentProfileRegistry`` / ``ToolSystem``）已收口到 ``app.config.configuration``，
由本模块做薄壳 re-export，使现有 ``Depends(get_agent_registry)`` / ``app.py`` 等调用点零改动；
运行时单例（``AgentRuntime``）因依赖 service 装配与 ``RuntimeContextBuilder``，由本模块自身
持有（``set_runtime`` / ``get_runtime`` / ``build_runtime``）。领域 service 与底层 CRUD/Store
单例由 ``app.service.depends`` 统一管理。
"""

from __future__ import annotations

from app.assistant_transport.service.conversation_run_executor import ConversationRunExecutor
from app.assistant_transport.service.conversation_task_snapshot_service import (
    ConversationTaskSnapshotService,
)
from app.assistant_transport.service.transport_assistant_service import TransportAssistantService
from app.config.configuration import (
    build_agent_registry,
    get_agent_registry,
    get_tool_system,
    set_agent_registry,
    set_tool_system,
)
from app.core.runtime.runner import AgentRuntime
from app.service import depends as service_depends
from app.service.log_query_service import LogQueryService
from app.service.provider import ModelEntryService, ProviderService
from app.assistant_transport.service.conversation_command_service import ConversationCommandService
from app.service.task.conversation_run_service import ConversationRunService
from app.service.task.conversation_run_workspace_resolver import ConversationRunWorkspaceResolver
from app.task_runtime.service.task_service import TaskService
from app.service.task.workspace_service import WorkspaceService
from app.service.workspace_event.workspace_event_bus import WorkspaceEventBus
from app.service.workspace_event.workspace_event_service import WorkspaceEventService
from app.storage.crud.workspace_readiness_crud import WorkspaceReadinessCrud

# 已迁移到 ``app.config.configuration`` 的进程级单例访问器，在此 re-export 以保持
# ``Depends(get_agent_registry)`` / ``app.py`` 等既有调用点零改动。
__all__ = [
    "build_agent_registry",
    "get_agent_registry",
    "get_model_entry_service",
    "get_provider_service",
    "get_tool_system",
    "set_agent_registry",
    "set_tool_system",
]

_RUNTIME: AgentRuntime | None = None


def _build_services() -> dict:
    """Build the process-wide domain service mapping.

    参数:
        无。后端运行配置由 ``Settings`` 类级静态属性提供，不以对象传入。

    返回:
        含 ``task_service`` / ``conversation_run_state_service`` / ``workspace_service`` /
        ``log_query_service`` 的字典。

    异常:
        RuntimeError: 如果应用启动尚未初始化 storage。

    副作用:
        具体 service 单例由 ``app.service.depends`` 缓存复用。
    """

    return {
        "task_service": service_depends.get_task_service(),
        "conversation_run_service": service_depends.get_conversation_run_service(),
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


def get_conversation_run_service() -> ConversationRunService:
    """返回进程级轮次 service 单例。

    参数:
        无。

    返回:
        ConversationRunStateService。

    异常:
        RuntimeError: 若存储初始化失败。

    副作用:
        首次调用时构建并缓存 service。
    """

    return _build_services()["conversation_run_service"]


def get_conversation_task_snapshot_service() -> ConversationTaskSnapshotService:
    """返回进程级 Task snapshot owner 单例。

    参数:
        无。

    返回:
        ConversationTaskSnapshotService，作为 Assistant Transport/UI state 的唯一事实源。

    异常:
        RuntimeError: 若存储初始化失败。

    副作用:
        首次调用时构建并缓存 service。
    """

    return service_depends.get_conversation_task_snapshot_service()



def get_conversation_run_executor() -> ConversationRunExecutor:
    """返回后台 Conversation Run 执行器（注入进程内取消注册表）。

    参数:
        无。

    返回:
        已装配 canonical writer 与取消信号源的 ConversationRunExecutor。

    异常:
        RuntimeError: 若存储初始化失败。

    副作用:
        首次调用时经 ``app.service.depends`` 构建并缓存单例；注入 core 的取消注册表
        单例是全工程唯一注入点，保证执行器取消编排的信号可见性。
    """

    return service_depends.get_conversation_run_executor()


def get_conversation_run_workspace_resolver() -> ConversationRunWorkspaceResolver:
    """返回进程级 turn → workspace 解析器单例。

    参数:
        无。

    返回:
        ConversationRunWorkspaceResolver。

    异常:
        RuntimeError: 若存储初始化失败。

    副作用:
        首次调用时构建并缓存 service。
    """

    return service_depends.get_conversation_run_workspace_resolver()


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


def get_workspace_readiness_crud() -> WorkspaceReadinessCrud:
    """返回 workspace readiness snapshot 的存储访问器。"""

    return service_depends.get_workspace_readiness_crud()


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


def set_runtime(runtime: AgentRuntime) -> None:
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


def get_runtime() -> AgentRuntime:
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
