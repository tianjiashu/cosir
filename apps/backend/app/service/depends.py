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
    from app.assistant_transport.service.conversation_task_state_service import (
        ConversationTaskStateService,
    )
    from app.assistant_transport.service.transport_assistant_service import (
        TransportAssistantService,
    )
    from app.core.workflow.conversation_run_ledger import ConversationRunLedger
    from app.core.workflow.message_queue.task_message_queue import TaskMessageQueue
    from app.service.conversation_run.conversation_task_context_service import (
        ConversationTaskContextService,
    )
    from app.service.conversation_run.tool_decision_service import ToolDecisionService
    from app.service.conversation_run.user_input_service import UserInputService
    from app.service.conversation_run.workspace_service import WorkspaceService
    from app.service.model_config import ModelConfigService
    from app.service.model_config.model_discovery_service import ModelDiscoveryService
    from app.service.recover_service import RecoverService
    from app.service.task_status.task_status_feed import TaskStatusFeed
    from app.service.workspace_files.file_watch import WorkspaceFileWatcher
    from app.service.workspace_files.workspace_files_service import WorkspaceFilesService
    from app.storage.crud.conversation_run_crud import ConversationRunCrud
    from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
    from app.storage.crud.model_config_crud import ModelConfigCrud
    from app.storage.crud.task_crud import TaskCrud
    from app.storage.crud.workspace_crud import WorkspaceCrud
    from app.task_runtime.service.task_service import TaskService
    from app.tools.mcp.connection_pool import MCPConnectionPool
    from app.tools.terminal_runtime.session_service import TerminalSessionService


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
    """关闭进程内运行时单例（含终端服务与 MCP 连接池）、清空 service 依赖缓存并关闭 SQLite 引擎。

    终端服务与 MCP 连接池只在这里经 ``reset_service_dependencies`` 关闭一次；lifespan 必须先关闭
    ``deferred_registry``，再调用本函数。
    """

    from app.storage.store_engines import close_storage
    from app.task_runtime.workspace_operation_registry import workspace_operations

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

    from app.tools.terminal_runtime.session_service import TerminalSessionService

    return TerminalSessionService()


@lru_cache(maxsize=1)
def get_mcp_connection_pool() -> MCPConnectionPool:
    """返回进程级 MCP 长连接池单例；循环线程与连接都在首次使用时才建立。"""

    from app.tools.mcp.connection_pool import MCPConnectionPool

    return MCPConnectionPool()


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

    from app.service.conversation_run.conversation_task_context_service import (
        ConversationTaskContextService,
    )

    return ConversationTaskContextService()


@lru_cache(maxsize=1)
def get_workspace_files_service() -> WorkspaceFilesService:
    """装配只读文件服务；工作区记录经装配层 crud 单例解析。"""
    from app.service.workspace_files.workspace_files_service import WorkspaceFilesService

    return WorkspaceFilesService()


@lru_cache(maxsize=1)
def get_task_status_feed() -> TaskStatusFeed:
    """进程级 Task 执行状态推送；Run 事件经 projector 触发发布。"""
    from app.service.task_status.task_status_feed import TaskStatusFeed

    return TaskStatusFeed()


@lru_cache(maxsize=1)
def get_workspace_file_watcher() -> WorkspaceFileWatcher:
    """监听器按订阅启动，由依赖关闭入口释放。"""
    from app.service.workspace_files.file_watch import WorkspaceFileWatcher

    return WorkspaceFileWatcher()


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
def get_conversation_run_ledger() -> ConversationRunLedger:
    """返回进程级 Conversation Run 事实台账单例。

    所有 Run 行的创建、读取、状态迁移、执行进度与结果改写都只经它，调用方不再区分
    「创建/查询」与「状态机」两套入口。

    返回:
        已装配 Task / Run CRUD 与会话工厂的 ``ConversationRunLedger`` 单例
        （``lru_cache`` 缓存）。

    异常:
        RuntimeError: 若存储初始化失败。
    """

    from app.core.workflow.conversation_run_ledger import ConversationRunLedger

    return ConversationRunLedger()


@lru_cache(maxsize=1)
def get_recover_service() -> RecoverService:
    """返回进程级启动恢复 service 单例。

    只负责后端启动期的重启恢复（收敛遗留 Run / TeamRun、拉起待调度 Task、延迟结果收尾）；
    运行期的 Run 生命周期归属 ``get_conversation_run_ledger``。
    """

    from app.service.recover_service import RecoverService

    return RecoverService()


@lru_cache(maxsize=1)
def get_task_message_queue() -> TaskMessageQueue:
    """返回进程级 Task 外部消息队列单例（无进程内状态，只包队列 CRUD）。"""

    from app.core.workflow.message_queue.task_message_queue import TaskMessageQueue

    return TaskMessageQueue()


@lru_cache(maxsize=1)
def get_user_input_service() -> UserInputService:
    """返回进程级用户输入解析服务单例（无进程内状态）。"""

    from app.service.conversation_run.user_input_service import UserInputService

    return UserInputService()


@lru_cache(maxsize=1)
def get_tool_decision_service() -> ToolDecisionService:
    """返回进程级 HITL 决定入队服务单例（无进程内状态）。"""

    from app.service.conversation_run.tool_decision_service import ToolDecisionService

    return ToolDecisionService()


@lru_cache(maxsize=1)
def get_conversation_event_projector() -> ConversationEventProjector:
    """返回进程级 conversation event → snapshot projector。"""
    from app.assistant_transport.service.conversation_event_projector import (
        ConversationEventProjector,
    )

    return ConversationEventProjector()


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

    if get_workspace_file_watcher.cache_info().currsize:
        get_workspace_file_watcher().close()
    get_workspace_file_watcher.cache_clear()
    if get_workspace_files_service.cache_info().currsize:
        get_workspace_files_service().close()
    get_workspace_files_service.cache_clear()
    _shutdown_cached_runtime_before_reset()
    ConversationTaskStateService.clear_process_state()
    get_workspace_service.cache_clear()
    get_conversation_run_ledger.cache_clear()
    get_recover_service.cache_clear()
    get_task_message_queue.cache_clear()
    get_user_input_service.cache_clear()
    get_tool_decision_service.cache_clear()
    get_task_service.cache_clear()
    get_workspace_crud.cache_clear()
    get_conversation_run_crud.cache_clear()
    get_task_crud.cache_clear()
    get_model_config_crud.cache_clear()
    get_conversation_task_context_crud.cache_clear()
    get_transport_assistant_service.cache_clear()
    get_conversation_event_projector.cache_clear()
    get_conversation_task_state_service.cache_clear()
    get_conversation_task_context_service.cache_clear()
    get_model_config_service.cache_clear()
    get_model_discovery_service.cache_clear()


def _shutdown_cached_runtime_before_reset() -> None:
    """丢弃缓存前关闭已构造的终端服务与 MCP 连接池（不顺手构造）；这是二者唯一的关闭点。

    关闭失败只记录，不阻断后续缓存清理与存储关闭。
    """

    if get_mcp_connection_pool.cache_info().currsize:
        try:
            get_mcp_connection_pool().close()
        except BaseException as exc:
            log.error(
                "service_dependency_mcp_pool_reset_failed",
                extra={
                    "msg": "service dependency reset 的 MCP 连接池关闭失败",
                    "data": {"error_type": type(exc).__name__, "error": str(exc)[:500]},
                },
            )
    get_mcp_connection_pool.cache_clear()

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
