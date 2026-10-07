"""Service-layer dependency accessors.

本模块是 service 层内部的轻量依赖装配入口：统一创建并缓存 storage CRUD/Store
单例，以及由这些存储对象支撑的 service 单例。API/Core 不应直接从这里取得 CRUD，
只应通过公开的 service 访问器拿业务服务。
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from app.config.logging.logger import log

if TYPE_CHECKING:
    from app.assistant_transport.service.conversation_event_projector import (
        ConversationEventProjector,
    )
    from app.assistant_transport.service.conversation_run_command_service import (
        ConversationRunCommandService,
    )
    from app.assistant_transport.service.conversation_task_state_service import (
        ConversationTaskStateService,
    )
    from app.assistant_transport.service.transport_assistant_service import (
        TransportAssistantService,
    )
    from app.core.runtime.conversation_run_executor import (
        ConversationRunExecutor,
    )
    from app.core.runtime.runner import AgentRuntime
    from app.service.model_config import ModelConfigService
    from app.service.model_config.model_discovery_service import ModelDiscoveryService
    from app.service.conversation_run.conversation_run_observability_service import (
        ConversationRunObservabilityService,
    )
    from app.service.conversation_run.conversation_run_service import ConversationRunService
    from app.service.conversation_run.conversation_run_state_service import ConversationRunStateService
    from app.service.conversation_run.conversation_task_context_service import ConversationTaskContextService
    from app.service.conversation_run.workspace_service import WorkspaceService
    from app.service.terminal.terminal_session_service import TerminalSessionService
    from app.storage.crud.conversation_run_crud import ConversationRunCrud
    from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
    from app.storage.crud.model_config_crud import ModelConfigCrud
    from app.storage.crud.task_crud import TaskCrud
    from app.storage.crud.workspace_crud import WorkspaceCrud
    from app.task_runtime.service.task_service import TaskService


_RUNTIME: AgentRuntime | None = None


def set_runtime(runtime: AgentRuntime) -> None:
    """设置进程级运行时单例。

    参数:
        runtime: 已构建的运行时实例，由应用启动时构建并注入。

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


def initialize_service_dependencies() -> None:
    """Initialize storage used by service dependencies.

    参数:
        无。

    返回:
        无。

    异常:
        OSError: 如果 SQLite 存储初始化失败。

    副作用:
        初始化应用数据库、日志数据库与相关目录。
    """

    from app.storage.store_engines import init_storage

    init_storage()


def close_service_dependencies() -> None:
    """Close storage used by service dependencies and clear cached singletons.

    参数:
        无。

    返回:
        无。

    异常:
        无。

    副作用:
        清空 service 依赖缓存并关闭 SQLite 存储引擎。
    """

    from app.storage.store_engines import close_storage
    from app.task_runtime.workspace_operation_registry import workspace_operations

    terminal_service = (
        get_terminal_session_service()
        if get_terminal_session_service.cache_info().currsize
        else None
    )
    if terminal_service is not None:
        terminal_service.shutdown()
    reset_service_dependencies()
    workspace_operations.close()
    close_storage()


@lru_cache(maxsize=1)
def get_task_crud() -> TaskCrud:
    """Return the process-local TaskCrud singleton.

    参数:
        无。

    返回:
        TaskCrud 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 TaskCrud。
    """

    from app.storage.crud.task_crud import TaskCrud

    return TaskCrud()


@lru_cache(maxsize=1)
def get_conversation_run_crud() -> ConversationRunCrud:
    """Return the process-local ConversationRunCrud singleton.

    参数:
        无。

    返回:
        ConversationRunCrud 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 ConversationRunCrud。
    """

    from app.storage.crud.conversation_run_crud import ConversationRunCrud

    return ConversationRunCrud()


@lru_cache(maxsize=1)
def get_workspace_crud() -> WorkspaceCrud:
    """Return the process-local WorkspaceCrud singleton.

    参数:
        无。

    返回:
        WorkspaceCrud 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 WorkspaceCrud。
    """

    from app.storage.crud.workspace_crud import WorkspaceCrud

    return WorkspaceCrud()


@lru_cache(maxsize=1)
def get_terminal_session_service() -> TerminalSessionService:
    """返回进程级 terminal session service 单例。"""

    from app.service.terminal.terminal_session_service import TerminalSessionService

    def observe_terminal_status(change: object) -> None:
        """把 PTY 生命周期变化转交给 Transport state owner。"""

        from app.service.terminal.session_status import TerminalSessionStatusChange

        if not isinstance(change, TerminalSessionStatusChange):
            raise TypeError("invalid terminal session status change")
        get_conversation_task_state_service().refresh_terminal_session(change)

    return TerminalSessionService(status_observer=observe_terminal_status)


@lru_cache(maxsize=1)
def get_task_service() -> TaskService:
    """Return the process-local TaskService singleton.

    参数:
        无。

    返回:
        TaskService 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 TaskService。
    """

    from app.task_runtime.service.task_service import TaskService

    return TaskService()


@lru_cache(maxsize=1)
def get_conversation_task_state_service() -> ConversationTaskStateService:
    """返回进程级 Task Transport state owner。"""

    from app.assistant_transport.service.conversation_task_state_service import (
        ConversationTaskStateService,
    )

    return ConversationTaskStateService()


@lru_cache(maxsize=1)
def get_conversation_task_context_service() -> ConversationTaskContextService:
    """返回进程级 Task Agent context owner。"""

    from app.service.conversation_run.conversation_task_context_service import ConversationTaskContextService

    return ConversationTaskContextService()


@lru_cache(maxsize=1)
def get_workspace_service() -> WorkspaceService:
    """Return the process-local WorkspaceService singleton.

    参数:
        无。

    返回:
        WorkspaceService 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 WorkspaceService。
    """

    from app.service.conversation_run.workspace_service import WorkspaceService

    return WorkspaceService()


@lru_cache(maxsize=1)
def get_model_config_crud() -> ModelConfigCrud:
    """返回进程级模型连接配置 CRUD 单例。"""

    from app.storage.crud.model_config_crud import ModelConfigCrud

    return ModelConfigCrud()


@lru_cache(maxsize=1)
def get_model_config_service() -> ModelConfigService:
    """返回进程级模型连接配置服务单例。"""

    from app.service.model_config import ModelConfigService

    return ModelConfigService()


@lru_cache(maxsize=1)
def get_model_discovery_service() -> ModelDiscoveryService:
    """返回进程级远端模型目录发现服务单例。"""

    from app.service.model_config.model_discovery_service import ModelDiscoveryService

    return ModelDiscoveryService()


@lru_cache(maxsize=1)
def get_conversation_task_context_crud() -> ConversationTaskContextCrud:
    """返回进程级 ConversationTaskContextCrud 单例。"""
    from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud

    return ConversationTaskContextCrud()


@lru_cache(maxsize=1)
def get_transport_assistant_service() -> TransportAssistantService:
    """返回进程级 Assistant Transport snapshot 订阅 service 单例。"""
    from app.assistant_transport.service.transport_assistant_service import (
        TransportAssistantService,
    )

    return TransportAssistantService()


@lru_cache(maxsize=1)
def get_conversation_run_command_service() -> ConversationRunCommandService:
    """返回进程级 Transport command 接收与 Conversation Run 编排 service 单例。"""

    from app.assistant_transport.service.conversation_run_command_service import (
        ConversationRunCommandService,
    )

    return ConversationRunCommandService()


@lru_cache(maxsize=1)
def get_conversation_run_service() -> ConversationRunService:
    """返回进程级 Conversation Run 用例编排 service 单例。

    只负责 Run 创建与编辑的编排（命令输入解析、附件处理、事务与事件发布所有权）；
    Run 状态迁移与查询见 ``get_conversation_run_state_service``。

    参数:
        无。

    返回:
        已装配 CRUD / context service / 会话工厂的 ``ConversationRunService`` 单例
        （``lru_cache`` 缓存）。

    异常:
        RuntimeError: 若存储初始化失败。
    """

    from app.service.conversation_run.conversation_run_service import ConversationRunService

    return ConversationRunService()


@lru_cache(maxsize=1)
def get_conversation_run_state_service() -> ConversationRunStateService:
    """返回进程级 Conversation Run 状态机单例。

    只负责 Run 状态的条件迁移、对应事件发布与状态查询；Run 创建/编辑编排见
    ``get_conversation_run_service``。

    参数:
        无。

    返回:
        已装配 run CRUD 与会话工厂的 ``ConversationRunStateService`` 单例
        （``lru_cache`` 缓存）。

    异常:
        RuntimeError: 若存储初始化失败。
    """

    from app.service.conversation_run.conversation_run_state_service import ConversationRunStateService

    return ConversationRunStateService()


@lru_cache(maxsize=1)
def get_conversation_run_observability_service() -> ConversationRunObservabilityService:
    """返回进程级 Conversation Run 可观测性事实 service。"""

    from app.service.conversation_run.conversation_run_observability_service import (
        ConversationRunObservabilityService,
    )

    return ConversationRunObservabilityService()



@lru_cache(maxsize=1)
def get_conversation_event_projector() -> ConversationEventProjector:
    """返回进程级 conversation event → snapshot projector。"""
    from app.assistant_transport.service.conversation_event_projector import (
        ConversationEventProjector,
    )

    return ConversationEventProjector()


@lru_cache(maxsize=1)
def get_conversation_run_executor() -> ConversationRunExecutor:
    """返回进程级后台运行执行器。

    无参数：执行器只依赖 run service。取消信号不再经 service 层注入端口——它由 core 的
    ``cancellation_registry`` 承载，由 ``ConversationRunExecutor.cancel`` 标记。

    返回:
        已装配 run service 的 ConversationRunExecutor 单例（``lru_cache`` 缓存）。

    异常:
        RuntimeError: 若存储初始化失败。
    """
    from app.core.runtime.conversation_run_executor import ConversationRunExecutor

    executor = ConversationRunExecutor()
    return executor


def reset_service_dependencies() -> None:
    """Clear service-layer dependency singletons.

    参数:
        无。

    返回:
        无。

    异常:
        无。

    副作用:
        清空本模块所有 lru_cache 单例；测试切换 storage 路径后应调用。
    """

    from app.assistant_transport.service.conversation_task_state_service import (
        ConversationTaskStateService,
    )

    _shutdown_cached_runtime_before_reset()
    ConversationTaskStateService.clear_process_state()
    get_workspace_service.cache_clear()
    get_conversation_run_service.cache_clear()
    get_conversation_run_state_service.cache_clear()
    get_conversation_run_observability_service.cache_clear()
    get_task_service.cache_clear()
    get_workspace_crud.cache_clear()
    get_conversation_run_crud.cache_clear()
    get_task_crud.cache_clear()
    get_model_config_crud.cache_clear()
    get_conversation_task_context_crud.cache_clear()
    get_conversation_run_command_service.cache_clear()
    get_conversation_run_state_service.cache_clear()
    get_transport_assistant_service.cache_clear()
    get_conversation_run_executor.cache_clear()
    get_conversation_event_projector.cache_clear()
    get_conversation_task_state_service.cache_clear()
    get_conversation_task_context_service.cache_clear()
    get_model_config_service.cache_clear()
    get_model_discovery_service.cache_clear()


def _shutdown_cached_runtime_before_reset() -> None:
    """Close process-local runtime owners before dropping their cached identities.

    ``reset_service_dependencies`` is synchronous and is used by tests and storage-path
    reconfiguration.  It therefore uses ``ConversationRunExecutor.close_sync`` as its explicit
    deterministic contract: terminal workers are fenced, task cancellation is posted to owner
    loops, and process-local indexes are cleared before caches are discarded.  The normal
    application lifespan uses the awaitable close path instead.
    """

    executor = None
    if get_conversation_run_executor.cache_info().currsize:
        executor = get_conversation_run_executor()
        try:
            executor.close_sync()
        except BaseException as exc:
            log.error(
                "service_dependency_runtime_reset_failed",
                extra={
                    "msg": "service dependency reset 的同步 runtime cleanup 失败",
                    "data": {"error_type": type(exc).__name__, "error": str(exc)[:500]},
                },
            )

    if get_terminal_session_service.cache_info().currsize:
        try:
            get_terminal_session_service().shutdown()
        except BaseException as exc:
            log.error(
                "service_dependency_terminal_reset_failed",
                extra={
                    "msg": "service dependency reset 的 terminal cleanup 失败",
                    "data": {"error_type": type(exc).__name__, "error": str(exc)[:500]},
                },
            )
    get_terminal_session_service.cache_clear()
