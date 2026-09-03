"""Service-layer dependency accessors.

本模块是 service 层内部的轻量依赖装配入口：统一创建并缓存 storage CRUD/Store
单例，以及由这些存储对象支撑的 service 单例。API/Core 不应直接从这里取得 CRUD，
只应通过公开的 service 访问器拿业务服务。
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from app.service.workspace_event.workspace_event_bus import WorkspaceEventBus

if TYPE_CHECKING:
    from app.assistant_transport.service.conversation_mutation_writer import (
        ConversationMutationWriter,
    )
    from app.assistant_transport.service.conversation_command_service import (
        ConversationCommandService,
    )
    from app.service.delegation.delegation_service import DelegationService
    from app.service.log_query_service import LogQueryService
    from app.service.provider import ModelEntryService, ProviderService
    from app.service.task.conversation_run_service import ConversationRunService
    from app.service.task.conversation_run_workspace_resolver import (
        ConversationRunWorkspaceResolver,
    )
    from app.service.task.conversation_state_service import ConversationStateService
    from app.service.task.task_service import TaskService
    from app.service.task.workspace_service import WorkspaceService
    from app.service.workspace_event.workspace_event_service import WorkspaceEventService
    from app.storage.cascade_deletion import CascadeDeleter
    from app.storage.crud.conversation_command_crud import ConversationCommandCrud
    from app.storage.crud.conversation_run_crud import ConversationRunCrud
    from app.storage.crud.delegation_crud import DelegationCrud
    from app.storage.crud.log_crud import LogCrud
    from app.storage.crud.model_entry_crud import ModelEntryCrud
    from app.storage.crud.provider_crud import ProviderCrud
    from app.storage.crud.task_crud import TaskCrud
    from app.storage.crud.workspace_crud import WorkspaceCrud
    from app.storage.crud.workspace_readiness_crud import WorkspaceReadinessCrud


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

    reset_service_dependencies()
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
def get_workspace_readiness_crud() -> WorkspaceReadinessCrud:
    """Return the process-local workspace readiness snapshot CRUD singleton."""

    from app.storage.crud.workspace_readiness_crud import WorkspaceReadinessCrud

    return WorkspaceReadinessCrud()


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
def get_cascade_deleter() -> CascadeDeleter:
    """Return the process-local CascadeDeleter singleton.

    参数:
        无。

    返回:
        CascadeDeleter 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 CascadeDeleter。
    """

    from app.storage.cascade_deletion import CascadeDeleter

    return CascadeDeleter()


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

    from app.service.task.task_service import TaskService

    return TaskService()


@lru_cache(maxsize=1)
def get_conversation_state_service() -> ConversationStateService:
    """Return the process-local ConversationStateService singleton.

    参数:
        无。

    返回:
        ConversationStateService 单例（投影 turns / turn_messages 为中性对话视图）。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 ConversationStateService（注入 turn service 与 turn message CRUD 单例）。
    """

    from app.service.task.conversation_state_service import ConversationStateService

    return ConversationStateService()


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
def get_workspace_event_bus() -> WorkspaceEventBus:
    """返回进程级 workspace 状态事件总线单例。

    参数:
        无。

    返回:
        WorkspaceEventBus 单例。

    异常:
        无。

    副作用:
        首次调用时创建 WorkspaceEventBus。
    """

    return WorkspaceEventBus()


def get_workspace_event_service() -> WorkspaceEventService | None:
    """返回 workspace 状态事件 service，CodeGraph 不可用时返回 None。

    因依赖 Kernel 进程状态（可能后启动/重启/不可用），不做缓存；每次构造轻量。
    CodeGraph 不可用时返回 None，调用方（API 层）据此降级返回 ready=False。

    参数:
        无。

    返回:
        WorkspaceEventService 实例；CodeGraph Kernel 不可用时返回 None。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        尝试从 supervisor 取得 Kernel client。
    """

    from app.codegraph import CodeGraphKernelUnavailableError, get_kernel_supervisor
    from app.service.codegraph_lifecycle_service import CodeGraphLifecycleService
    from app.service.workspace_event.workspace_event_service import WorkspaceEventService
    from app.storage.crud.workspace_readiness_crud import WorkspaceReadinessCrud

    try:
        client = get_kernel_supervisor().get_client()
    except (RuntimeError, CodeGraphKernelUnavailableError):
        # supervisor 未初始化或 Kernel 未就绪：禁用索引准备，降级到文件搜索。
        return None
    return WorkspaceEventService(
        lifecycle=CodeGraphLifecycleService(client),
        bus=get_workspace_event_bus(),
        readiness_crud=WorkspaceReadinessCrud(),
    )


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
def get_conversation_command_service() -> ConversationCommandService:
    """返回进程级 command 编排 service 单例。"""
    from app.assistant_transport.service.conversation_command_service import (
        ConversationCommandService,
    )

    return ConversationCommandService()


@lru_cache(maxsize=1)
def get_conversation_run_service() -> ConversationRunService:
    """返回进程级 ConversationRun 状态与执行 service 单例。"""
    from app.service.task.conversation_run_service import ConversationRunService

    return ConversationRunService()


@lru_cache(maxsize=1)
def get_conversation_mutation_writer() -> ConversationMutationWriter:
    """返回进程级 Conversation Facts 写入器单例。

    参数:
        无。

    返回:
        使用主库共享引擎的 ``ConversationMutationWriter``。

    异常:
        RuntimeError: 如果主库存储尚未初始化。

    副作用:
        首次调用时创建 Writer。
    """

    from app.assistant_transport.service.conversation_mutation_writer import (
        ConversationMutationWriter,
    )

    return ConversationMutationWriter()


@lru_cache(maxsize=1)
def get_conversation_run_executor():
    """返回进程级后台运行执行器。"""
    from app.assistant_transport.service.conversation_run_executor import ConversationRunExecutor

    return ConversationRunExecutor(
        get_conversation_mutation_writer(),
    )


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

    get_log_query_service.cache_clear()
    get_workspace_event_bus.cache_clear()
    get_workspace_service.cache_clear()
    get_conversation_run_workspace_resolver.cache_clear()
    get_conversation_run_service.cache_clear()
    get_task_service.cache_clear()
    get_log_crud.cache_clear()
    get_delegation_service.cache_clear()
    get_delegation_crud.cache_clear()
    get_cascade_deleter.cache_clear()
    get_workspace_crud.cache_clear()
    get_workspace_readiness_crud.cache_clear()
    get_conversation_run_crud.cache_clear()
    get_task_crud.cache_clear()
    get_provider_crud.cache_clear()
    get_model_entry_crud.cache_clear()
    get_conversation_command_crud.cache_clear()
    get_conversation_run_service.cache_clear()
    get_conversation_command_service.cache_clear()
    get_conversation_run_executor.cache_clear()
    get_conversation_mutation_writer.cache_clear()
    get_provider_service.cache_clear()
    get_model_entry_service.cache_clear()
