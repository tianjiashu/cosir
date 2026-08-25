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
    from app.service.agent_runtime_event.runtime_event_bus import RuntimeEventBus
    from app.service.agent_runtime_event.runtime_event_service import RuntimeEventService
    from app.service.delegation.delegation_service import DelegationService
    from app.llm_provider.model_resolver_service import ModelResolverService
    from app.service.log_query_service import LogQueryService
    from app.llm_provider.provider import ModelEntryService
    from app.llm_provider.provider.provider_connection_test_service import (
        ProviderConnectionTestService,
    )
    from app.llm_provider.provider import ProviderDiscoverService
    from app.llm_provider.provider import ProviderService
    from app.service.task.task_service import TaskService
    from app.service.task.turn_service import TurnService
    from app.service.task.turn_stream_service import TurnStreamService
    from app.service.task.turn_workspace_resolver import TurnWorkspaceResolver
    from app.service.task.workspace_service import WorkspaceService
    from app.service.workspace_event.workspace_event_service import WorkspaceEventService
    from app.storage.cascade_deletion import CascadeDeleter
    from app.storage.crud.delegation_crud import DelegationCrud
    from app.storage.crud.log_crud import LogCrud
    from app.storage.crud.model_entry_crud import ModelEntryCrud
    from app.storage.crud.provider_crud import ProviderCrud
    from app.storage.crud.runtime_event_crud import RuntimeEventCrud
    from app.storage.crud.task_crud import TaskCrud
    from app.storage.crud.turn_crud import TurnCrud
    from app.storage.crud.turn_message_crud import TurnMessageCrud
    from app.storage.crud.workspace_crud import WorkspaceCrud


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
def get_turn_crud() -> TurnCrud:
    """Return the process-local TurnCrud singleton.

    参数:
        无。

    返回:
        TurnCrud 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 TurnCrud。
    """

    from app.storage.crud.turn_crud import TurnCrud

    return TurnCrud()


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
def get_runtime_event_crud() -> RuntimeEventCrud:
    """Return the process-local RuntimeEventCrud singleton.

    参数:
        无。

    返回:
        RuntimeEventCrud 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 RuntimeEventCrud。
    """

    from app.storage.crud.runtime_event_crud import RuntimeEventCrud

    return RuntimeEventCrud()


@lru_cache(maxsize=1)
def get_turn_message_crud() -> TurnMessageCrud:
    """Return the process-local TurnMessageCrud singleton.

    参数:
        无。

    返回:
        TurnMessageCrud 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 TurnMessageCrud。
    """

    from app.storage.crud.turn_message_crud import TurnMessageCrud

    return TurnMessageCrud()


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
        首次调用时创建 DelegationService，并复用 delegation CRUD 与 runtime event service。
    """

    from app.service.delegation.delegation_service import DelegationService

    return DelegationService(
        delegation_crud=get_delegation_crud(),
        runtime_event_service=get_runtime_event_service(),
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
def get_runtime_event_bus() -> RuntimeEventBus:
    """Return the process-local runtime event bus singleton.

    参数:
        无。

    返回:
        RuntimeEventBus 单例。

    异常:
        无。

    副作用:
        首次调用时创建 RuntimeEventBus。
    """

    from app.service.agent_runtime_event.runtime_event_bus import RuntimeEventBus

    return RuntimeEventBus()


@lru_cache(maxsize=1)
def get_runtime_event_service() -> RuntimeEventService:
    """Return the process-local RuntimeEventService singleton.

    参数:
        无。

    返回:
        RuntimeEventService 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 RuntimeEventService。
    """

    from app.service.agent_runtime_event.runtime_event_service import RuntimeEventService

    return RuntimeEventService()


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
def get_turn_service() -> TurnService:
    """Return the process-local TurnService singleton.

    参数:
        无。

    返回:
        TurnService 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 TurnService。
    """

    from app.service.task.turn_service import TurnService

    return TurnService()


@lru_cache(maxsize=1)
def get_turn_stream_service() -> TurnStreamService:
    """Return the process-local TurnStreamService singleton.

    参数:
        无。

    返回:
        TurnStreamService 单例（无状态编排器，事件总线 / 轮次 service /
        运行时事件 service 三个稳定单例在构造时注入）。

    异常:
        RuntimeError: 如果 storage 尚未初始化（经由 turn service / runtime event service）。

    副作用:
        首次调用时创建 TurnStreamService。
    """

    from app.service.task.turn_stream_service import TurnStreamService

    return TurnStreamService(
        event_bus=get_runtime_event_bus(),
        turn_service=get_turn_service(),
        runtime_event_service=get_runtime_event_service(),
    )


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
def get_turn_workspace_resolver() -> TurnWorkspaceResolver:
    """Return the process-local TurnWorkspaceResolver singleton.

    参数:
        无。

    返回:
        TurnWorkspaceResolver 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 TurnWorkspaceResolver。
    """

    from app.service.task.turn_workspace_resolver import TurnWorkspaceResolver

    return TurnWorkspaceResolver()


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

    try:
        client = get_kernel_supervisor().get_client()
    except (RuntimeError, CodeGraphKernelUnavailableError):
        # supervisor 未初始化或 Kernel 未就绪：禁用索引准备，降级到文件搜索。
        return None
    return WorkspaceEventService(
        lifecycle=CodeGraphLifecycleService(client),
        bus=get_workspace_event_bus(),
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



    return ProviderService()


@lru_cache(maxsize=1)
def get_provider_discover_service() -> ProviderDiscoverService:
    """返回进程级 ProviderDiscoverService 单例。

    参数:
        无。

    返回:
        ProviderDiscoverService 单例。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 ProviderDiscoverService（注入 ModelEntryCrud 单例）。
    """



    return ProviderDiscoverService()


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



    return ModelEntryService()


@lru_cache(maxsize=1)
def get_model_resolver_service() -> ModelResolverService:
    """返回进程级 ModelResolverService 单例（依赖 ModelEntryCrud 与 ProviderService 单例）。

    参数:
        无。

    返回:
        ModelResolverService 单例（依赖 ModelEntryCrud 与 ProviderService 单例）。

    异常:
        RuntimeError: 如果 storage 尚未初始化。

    副作用:
        首次调用时创建 ModelResolverService。
    """

    from app.llm_provider.model_resolver_service import ModelResolverService

    return ModelResolverService()


@lru_cache(maxsize=1)
def get_provider_connection_test_service() -> ProviderConnectionTestService:
    """返回进程级 ProviderConnectionTestService 单例（设计文档 §七 阶段 2）。

    参数:
        无。

    返回:
        ProviderConnectionTestService 单例（阶段 2 实施时按需注入
        ProviderService / ModelResolverService 单例，本期骨架无构造依赖）。

    异常:
        RuntimeError: 如果 storage 尚未初始化（阶段 2 实施后接入）。

    副作用:
        首次调用时创建 ProviderConnectionTestService。
    """

    return ProviderConnectionTestService()


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
    get_turn_workspace_resolver.cache_clear()
    get_turn_service.cache_clear()
    get_turn_stream_service.cache_clear()
    get_task_service.cache_clear()
    get_runtime_event_service.cache_clear()
    get_runtime_event_bus.cache_clear()
    get_log_crud.cache_clear()
    get_delegation_service.cache_clear()
    get_turn_message_crud.cache_clear()
    get_delegation_crud.cache_clear()
    get_cascade_deleter.cache_clear()
    get_runtime_event_crud.cache_clear()
    get_workspace_crud.cache_clear()
    get_turn_crud.cache_clear()
    get_task_crud.cache_clear()
    get_provider_crud.cache_clear()
    get_model_entry_crud.cache_clear()
    get_provider_service.cache_clear()
    get_provider_discover_service.cache_clear()
    get_model_entry_service.cache_clear()
    get_model_resolver_service.cache_clear()
    get_provider_connection_test_service.cache_clear()
