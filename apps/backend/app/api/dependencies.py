"""FastAPI 层的依赖构建器。"""

from app.domain.approvals.service import ApprovalService
from app.domain.approvals.store import ApprovalStore
from app.domain.artifacts.files import ArtifactFileStore
from app.domain.artifacts.service import ArtifactService
from app.domain.artifacts.store import ArtifactStore
from app.config.settings import default_settings
from app.context.builder import TextContextBuilder
from app.core.logs.query_service import LogQueryService
from app.core.replay.service import ReplayService
from app.core.trace.query_service import TraceQueryService
from app.core.trace.recorder import TraceRecorder
from app.config.logging import install_logging_for_current_process
from app.models.factory import build_model_adapter
from app.core.runs.checkpointer import LangGraphCheckpointerUnavailable
from app.core.runs.langgraph_runtime import LangGraphRuntime
from app.core.runs.lifecycle_graph import build_run_lifecycle_graph
from app.core.runs.recovery import RecoveryManager
from app.core.runs.resume import ResumeDispatcher
from app.core.runs.store import DurableRunStore
from app.core.runtime.runner import AgentRuntime
from app.storage.sqlite import SQLiteTaskStore
from app.storage.log_store import LogStore
from app.storage.trace_store import TraceStore
from app.tools.execution.policy import ToolExecutionPolicy
from app.tools.execution.policy_provider import PermissionPolicyProvider
from app.tools.execution.service import ToolExecutionService
from app.tools.execution.store import ToolExecutionStore
from app.tools.runtime.concurrency import ToolConcurrentScheduler
from app.tools.executor import ToolCallExecutor
from app.tools.runtime.locks import ToolResourceLockManager
from app.tools.registry.memory import ToolRegistry
from app.tools.results import ToolObservationBuilder
from app.tools.runtime.platform import ToolRuntime
from app.tools.builtin.safe_read import SafeReadTools
from app.tools.runtime.compatibility import ToolScheduler


_RUNTIME: AgentRuntime | None = None


def set_runtime(runtime: AgentRuntime) -> None:
    """设置当前进程共享的运行时单例。

    参数:
        runtime: 已构建的 AgentRuntime 实例。

    返回:
        无。

    异常:
        无。

    副作用:
        覆盖模块级运行时单例，供 FastAPI 依赖注入使用。
    """

    global _RUNTIME
    _RUNTIME = runtime


def get_runtime() -> AgentRuntime:
    """返回当前进程的运行时单例。

    参数:
        无。

    返回:
        已设置的 AgentRuntime 实例。

    异常:
        RuntimeError: 当运行时尚未通过 set_runtime 或 create_app 初始化时抛出。

    副作用:
        无。
    """

    if _RUNTIME is None:
        raise RuntimeError("runtime has not been initialized")
    return _RUNTIME


def build_runtime() -> AgentRuntime:
    """构建默认的运行时依赖图。

    参数:
        无。

    返回:
        接入本地配置、文件日志、SQLite 存储、文本上下文构建、安全读取工具，
        以及所配置的流式模型适配器的 AgentRuntime。

    异常:
        OSError: 当日志器或 SQLite 存储无法创建本地文件时抛出。

    副作用:
        配置文件日志并初始化 SQLite schema。
    """

    settings = default_settings()
    logger = install_logging_for_current_process(
        log_dir=settings.log_dir,
        log_database_file=settings.log_database_file,
        sqlite_logging_enabled=settings.sqlite_logging_enabled,
        queue_size=settings.log_queue_size,
        batch_size=settings.log_batch_size,
        flush_interval_ms=settings.log_flush_interval_ms,
    )
    log_query_service = _build_log_query_service(settings, logger)
    trace_store = TraceStore(settings.database_file)
    trace_recorder = TraceRecorder(trace_store, logger)
    trace_query_service = TraceQueryService(trace_store, settings.log_dir)
    replay_service = ReplayService(trace_store)
    safe_tools = SafeReadTools(settings.project_root)
    registry = ToolRegistry(safe_tools.definitions())
    run_store = DurableRunStore(settings.database_file, logger)
    resume_dispatcher = ResumeDispatcher(run_store, logger, trace_recorder=trace_recorder)
    langgraph_runtime = None
    try:
        langgraph_runtime = LangGraphRuntime(
            database_path=settings.database_file.with_name("langgraph.sqlite3"),
            graph_factory=build_run_lifecycle_graph,
        )
    except (LangGraphCheckpointerUnavailable, RuntimeError, OSError) as exc:
        logger.warning("langgraph_runtime_unavailable", extra={"error": str(exc)})
    approval_service = ApprovalService(
        approval_store=ApprovalStore(settings.database_file),
        run_store=run_store,
        resume_dispatcher=resume_dispatcher,
        logger=logger,
        langgraph_runtime=langgraph_runtime,
        trace_recorder=trace_recorder,
    )
    observation_builder = ToolObservationBuilder()
    execution_service = ToolExecutionService(
        store=ToolExecutionStore(settings.database_file),
        policy=ToolExecutionPolicy(
            (
                PermissionPolicyProvider(
                    auto_approved_permissions=("safe_read",),
                    approval_required_permissions=("write_file", "command", "git_write"),
                ),
            )
        ),
        logger=logger,
    )
    artifact_service = ArtifactService(
        store=ArtifactStore(settings.database_file),
        files=ArtifactFileStore(settings.project_root / "storage" / "artifacts"),
    )
    tool_runtime = ToolRuntime(
        registry=registry,
        executor=ToolCallExecutor(
            observation_builder=observation_builder,
            logger=logger,
            artifact_service=artifact_service,
            execution_service=execution_service,
        ),
        observation_builder=observation_builder,
        logger=logger,
        execution_service=execution_service,
        approval_service=approval_service,
        concurrent_scheduler=ToolConcurrentScheduler(ToolResourceLockManager(), logger),
    )
    return AgentRuntime(
        settings=settings,
        task_store=SQLiteTaskStore(settings.database_file),
        context_builder=TextContextBuilder(),
        model_adapter=build_model_adapter(settings),
        tool_scheduler=ToolScheduler(
            registry=registry,
            allowed_permissions=("safe_read",),
            logger=logger,
        ),
        logger=logger,
        run_store=run_store,
        approval_service=approval_service,
        recovery_manager=RecoveryManager(run_store, logger, trace_recorder=trace_recorder),
        resume_dispatcher=resume_dispatcher,
        langgraph_runtime=langgraph_runtime,
        tool_runtime=tool_runtime,
        trace_recorder=trace_recorder,
        trace_query_service=trace_query_service,
        replay_service=replay_service,
        log_query_service=log_query_service,
    )


def _build_log_query_service(settings, logger):
    """构建日志查询服务，SQLite 日志库不可用时降级为空服务。

    参数:
        settings: 后端运行配置。
        logger: 后端主日志器。

    返回:
        LogQueryService 或 None。

    异常:
        无。日志 SQLite 初始化失败会记录 warning 并返回 None。

    副作用:
        成功时初始化日志 SQLite schema；失败时写文件日志。
    """

    try:
        log_store = LogStore(settings.log_database_file)
    except Exception as exc:
        logger.warning("log_query_service_unavailable", extra={"error": str(exc)})
        return None
    return LogQueryService(log_store, max_limit=settings.log_query_limit_max)
