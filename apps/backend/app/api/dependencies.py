"""FastAPI dependency wiring."""

from app.config.logging import install_logging_for_current_process
from app.config.settings import default_settings
from app.context.builder import TextContextBuilder
from app.core.logs.query_service import LogQueryService
from app.core.runtime.runner import AgentRuntime
from app.core.trace.query_service import TraceQueryService
from app.core.trace.recorder import TraceRecorder
from app.models.factory import build_model_adapter
from app.storage.crud.durable import DurableRunStore
from app.storage.crud.log import LogStore
from app.storage.crud.task import SQLiteTaskStore
from app.storage.crud.trace import TraceStore
from app.tools.registry.tool_registry import ToolRegistry
from app.tools.runtime.compatibility import ToolScheduler
from app.tools.tool_handler.safe_read import SafeReadTools

_RUNTIME: AgentRuntime | None = None


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


def build_runtime() -> AgentRuntime:
    """Build the default runtime dependency graph.

    Parameters:
        None.

    Returns:
        Configured AgentRuntime instance.

    Raises:
        OSError: If logs or SQLite storage cannot be created.

    Side effects:
        Configures logging and initializes SQLite-backed stores.
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
    safe_tools = SafeReadTools(settings.project_root)
    registry = ToolRegistry(safe_tools.definitions())
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
        run_store=DurableRunStore(settings.database_file),
        trace_recorder=trace_recorder,
        trace_query_service=trace_query_service,
        log_query_service=log_query_service,
    )


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
        log_store = LogStore(settings.log_database_file)
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
