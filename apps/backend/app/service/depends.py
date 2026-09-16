"""Service-layer dependency accessors.

本模块是 service 层内部的轻量依赖装配入口：统一创建并缓存 storage CRUD/Store
单例，以及由这些存储对象支撑的 service 单例。API/Core 不应直接从这里取得 CRUD，
只应通过公开的 service 访问器拿业务服务。
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.assistant_transport.service.conversation_event_projector import (
        ConversationEventProjector,
    )
    from app.assistant_transport.service.conversation_run_command_service import (
        ConversationRunCommandService,
    )
    from app.assistant_transport.service.conversation_run_executor import (
        ConversationRunExecutor,
    )
    from app.assistant_transport.service.conversation_task_state_service import (
        ConversationTaskStateService,
    )
    from app.assistant_transport.service.transport_assistant_service import (
        TransportAssistantService,
    )
    from app.core.runtime.runner import AgentRuntime
    from app.service.delegation.delegation_service import DelegationService
    from app.service.log_query_service import LogQueryService
    from app.service.provider import ModelEntryService, ProviderService
    from app.service.task.conversation_run_service import ConversationRunService
    from app.service.task.conversation_run_state_service import ConversationRunStateService
    from app.service.task.conversation_run_workspace_resolver import (
        ConversationRunWorkspaceResolver,
    )
    from app.service.task.conversation_task_context_service import ConversationTaskContextService
    from app.service.task.workspace_service import WorkspaceService
    from app.service.terminal.terminal_session_service import TerminalSessionService
    from app.storage.crud.conversation_command_crud import ConversationCommandCrud
    from app.storage.crud.conversation_run_crud import ConversationRunCrud
    from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
    from app.storage.crud.delegation_crud import DelegationCrud
    from app.storage.crud.file_snapshot_crud import FileSnapshotCrud
    from app.storage.crud.log_crud import LogCrud
    from app.storage.crud.model_entry_crud import ModelEntryCrud
    from app.storage.crud.provider_crud import ProviderCrud
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
def get_delegation_crud() -> DelegationCrud:
    """Return the process-local DelegationCrud singleton.

    参数:
        无。

    返回:
        DelegationCrud 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 DelegationCrud。
    """

    from app.storage.crud.delegation_crud import DelegationCrud

    return DelegationCrud()


@lru_cache(maxsize=1)
def get_delegation_service() -> DelegationService:
    """Return the process-local DelegationService singleton.

    参数:
        无。

    返回:
        DelegationService 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 DelegationService，并复用 delegation CRUD。
    """

    from app.service.delegation.delegation_service import DelegationService

    return DelegationService(
        delegation_crud=get_delegation_crud(),
    )


@lru_cache(maxsize=1)
def get_terminal_session_service() -> TerminalSessionService:
    """返回进程级 terminal session service 单例。"""

    from app.service.terminal.terminal_session_service import TerminalSessionService
    from app.storage.crud.terminal_session_crud import TerminalSessionCrud

    return TerminalSessionService(crud=TerminalSessionCrud())


@lru_cache(maxsize=1)
def get_log_crud() -> LogCrud:
    """Return the process-local LogCrud singleton.

    参数:
        无。

    返回:
        LogCrud 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 LogCrud。
    """

    from app.storage.crud.log_crud import LogCrud

    return LogCrud()


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

    from app.service.task.conversation_task_context_service import ConversationTaskContextService

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

    from app.service.task.workspace_service import WorkspaceService

    return WorkspaceService()


@lru_cache(maxsize=1)
def get_conversation_run_workspace_resolver() -> ConversationRunWorkspaceResolver:
    """Return the process-local ConversationRunWorkspaceResolver singleton.

    参数:
        无。

    返回:
        ConversationRunWorkspaceResolver 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 ConversationRunWorkspaceResolver。
    """

    from app.service.task.conversation_run_workspace_resolver import (
        ConversationRunWorkspaceResolver,
    )

    return ConversationRunWorkspaceResolver()


@lru_cache(maxsize=1)
def get_log_query_service() -> LogQueryService:
    """Return the process-local LogQueryService singleton.

    参数:
        无。

    返回:
        LogQueryService 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 LogQueryService。
    """

    from app.service.log_query_service import LogQueryService

    return LogQueryService()


@lru_cache(maxsize=1)
def get_provider_crud() -> ProviderCrud:
    """返回进程级 ProviderCrud 单例。

    参数:
        无。

    返回:
        ProviderCrud 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 ProviderCrud。
    """

    from app.storage.crud.provider_crud import ProviderCrud

    return ProviderCrud()


@lru_cache(maxsize=1)
def get_conversation_command_crud() -> ConversationCommandCrud:
    """返回进程级 ConversationCommandCrud 单例。"""
    from app.storage.crud.conversation_command_crud import ConversationCommandCrud

    return ConversationCommandCrud()


@lru_cache(maxsize=1)
def get_conversation_task_context_crud() -> ConversationTaskContextCrud:
    """返回进程级 ConversationTaskContextCrud 单例。"""
    from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud

    return ConversationTaskContextCrud()


@lru_cache(maxsize=1)
def get_file_snapshot_crud() -> FileSnapshotCrud:
    """返回进程级 FileSnapshotCrud 单例。"""
    from app.storage.crud.file_snapshot_crud import FileSnapshotCrud

    return FileSnapshotCrud()


@lru_cache(maxsize=1)
def get_transport_assistant_service() -> TransportAssistantService:
    """返回进程级 Assistant Transport snapshot 订阅 service 单例。"""
    from app.assistant_transport.service.transport_assistant_service import (
        TransportAssistantService,
    )

    return TransportAssistantService()


@lru_cache(maxsize=1)
def get_conversation_run_command_service() -> ConversationRunCommandService:
    """返回进程级 command 幂等与 Conversation Run 创建 service 单例。"""

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

    from app.service.task.conversation_run_service import ConversationRunService

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

    from app.service.task.conversation_run_state_service import ConversationRunStateService

    return ConversationRunStateService()


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
    from app.assistant_transport.service.conversation_run_executor import ConversationRunExecutor

    return ConversationRunExecutor()


@lru_cache(maxsize=1)
def get_model_entry_crud() -> ModelEntryCrud:
    """返回进程级 ModelEntryCrud 单例。

    参数:
        无。

    返回:
        ModelEntryCrud 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 ModelEntryCrud。
    """

    from app.storage.crud.model_entry_crud import ModelEntryCrud

    return ModelEntryCrud()


@lru_cache(maxsize=1)
def get_provider_service() -> ProviderService:
    """返回进程级 ProviderService 单例。

    参数:
        无。

    返回:
        ProviderService 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 ProviderService（注入 ProviderCrud 单例）。
    """

    from app.service.provider import ProviderService

    return ProviderService()


@lru_cache(maxsize=1)
def get_model_entry_service() -> ModelEntryService:
    """返回进程级 ModelEntryService 单例。

    参数:
        无。

    返回:
        ModelEntryService 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 ModelEntryService（注入 ModelEntryCrud 单例）。
    """

    from app.service.provider import ModelEntryService

    return ModelEntryService()


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

    ConversationTaskStateService.clear_process_state()
    get_log_query_service.cache_clear()
    get_workspace_service.cache_clear()
    get_conversation_run_workspace_resolver.cache_clear()
    get_conversation_run_service.cache_clear()
    get_conversation_run_state_service.cache_clear()
    get_task_service.cache_clear()
    get_log_crud.cache_clear()
    get_delegation_service.cache_clear()
    get_delegation_crud.cache_clear()
    get_workspace_crud.cache_clear()
    get_conversation_run_crud.cache_clear()
    get_task_crud.cache_clear()
    get_provider_crud.cache_clear()
    get_model_entry_crud.cache_clear()
    get_conversation_command_crud.cache_clear()
    get_conversation_task_context_crud.cache_clear()
    get_file_snapshot_crud.cache_clear()
    get_conversation_run_command_service.cache_clear()
    get_conversation_run_state_service.cache_clear()
    get_transport_assistant_service.cache_clear()
    get_conversation_run_executor.cache_clear()
    get_conversation_event_projector.cache_clear()
    get_conversation_task_state_service.cache_clear()
    get_conversation_task_context_service.cache_clear()
    get_provider_service.cache_clear()
    get_model_entry_service.cache_clear()
    get_terminal_session_service.cache_clear()
