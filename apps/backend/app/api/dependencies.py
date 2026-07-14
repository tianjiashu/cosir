"""FastAPI 层的依赖构建器。"""

from app.domain.approvals.service import ApprovalService
from app.domain.approvals.store import ApprovalStore
from app.domain.artifacts.files import ArtifactFileStore
from app.domain.artifacts.service import ArtifactService
from app.domain.artifacts.store import ArtifactStore
from app.config.settings import default_settings
from app.context.builder import TextContextBuilder
from app.logging.configuration import configure_logging
from app.models.factory import build_model_adapter
from app.core.runs.checkpointer import LangGraphCheckpointerUnavailable
from app.core.runs.langgraph_runtime import LangGraphRuntime
from app.core.runs.lifecycle_graph import build_run_lifecycle_graph
from app.core.runs.recovery import RecoveryManager
from app.core.runs.resume import ResumeDispatcher
from app.core.runs.store import DurableRunStore
from app.core.runtime.runner import AgentRuntime
from app.storage.sqlite import SQLiteTaskStore
from app.tools.execution.policy import ToolExecutionPolicy
from app.tools.execution.policy_provider import PermissionPolicyProvider
from app.tools.execution.service import ToolExecutionService
from app.tools.execution.store import ToolExecutionStore
from app.tools.concurrent import ToolConcurrentScheduler
from app.tools.executor import ToolCallExecutor
from app.tools.locks import ToolResourceLockManager
from app.tools.registry import ToolRegistry
from app.tools.results import ToolObservationBuilder
from app.tools.runtime import ToolRuntime
from app.tools.safe_read import SafeReadTools
from app.tools.scheduler import ToolScheduler


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
    logger = configure_logging(settings.log_file)
    safe_tools = SafeReadTools(settings.project_root)
    registry = ToolRegistry(safe_tools.definitions())
    run_store = DurableRunStore(settings.database_file, logger)
    resume_dispatcher = ResumeDispatcher(run_store, logger)
    langgraph_runtime = None
    try:
        langgraph_runtime = LangGraphRuntime(
            database_path=settings.database_file.with_name("langgraph.sqlite3"),
            graph_factory=build_run_lifecycle_graph,
        )
    except (LangGraphCheckpointerUnavailable, RuntimeError, OSError) as exc:
        logger.warning("langgraph_runtime_unavailable error=%s", exc)
    approval_service = ApprovalService(
        approval_store=ApprovalStore(settings.database_file),
        run_store=run_store,
        resume_dispatcher=resume_dispatcher,
        logger=logger,
        langgraph_runtime=langgraph_runtime,
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
        recovery_manager=RecoveryManager(run_store, logger),
        langgraph_runtime=langgraph_runtime,
        tool_runtime=tool_runtime,
    )
