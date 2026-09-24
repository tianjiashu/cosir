"""后端应用生命周期编排。

本模块是后端进程启动与关闭阶段的唯一编排点：FastAPI ``lifespan``、启动步骤顺序、
关闭时的资源释放顺序，以及 ``ready`` / ``failed`` / ``stopped`` 启动状态上报都
收口在这里。应用装配（FastAPI 实例、CORS、请求日志中间件、域路由注册）留在
``app.app``。

职责边界：

- 负责：日志管线安装、``Settings`` 加载、服务依赖初始化、遗留 Run 与委派收敛、
  Hook 注册表播种、工具系统与 Agent Runtime 装配、``SESSION_START`` /
  ``SESSION_END`` 触发、启动状态标记、系统级 ``.cosir`` 目录创建。
- 不负责：HTTP 路由、中间件安装、FastAPI 实例的创建与导出。

本模块在 ``app.app`` 导入期被加载，因此这里的顶层 import 都发生在应用装配阶段；
新增依赖前须确认它不会反向导入 ``app.app``，否则会形成循环导入。
"""

import asyncio
import json
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.assistant_transport.event.tool_runtime_output_adapter import (
    ToolRuntimeOutputChannelFactory,
)
from app.bootstate import (
    boot_state_file_from_env,
    write_bootstate,
)
from app.config.configuration import (
    build_agent_registry,
    set_agent_registry,
    set_tool_system,
)
from app.config.constant import Constant
from app.config.logging.configuration import install_logging_for_current_process, shutdown_logging
from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.hook import HookContext, HookEvent, HookInterceptor
from app.core.observability import flush_langfuse
from app.core.runtime.runner import AgentRuntime
from app.core.tools import ToolSystem
from app.core.workflows.react.workflow import ReactLikeWorkflow
from app.service.depends import (
    close_service_dependencies,
    get_conversation_run_executor,
    get_conversation_run_service,
    get_terminal_session_service,
    initialize_service_dependencies,
    set_runtime,
)
from app.utils import paths
from app.utils.cosir_paths import system_cosir_dir


def get_delegation_service() -> None:
    """Compatibility test seam; production startup no longer owns delegation storage."""

    return None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """包装生命周期，使 yield 前的启动异常也能写入 bootstate。"""
    try:
        async with _lifespan_impl(_app):
            yield
    except BaseException as exc:
        _mark_boot_failed(exc)
        await _cleanup_startup_failure()
        raise
    finally:
        # Startup can fail before _lifespan_impl reaches its normal shutdown block.
        # Close any file/queue handlers created before that failure as well.
        await asyncio.to_thread(shutdown_logging)


async def _cleanup_startup_failure() -> None:
    """Release every resource that may have been initialized before startup failed.

    Startup is intentionally not wrapped in the normal post-``yield`` ``finally`` block, so a
    failure during AgentRuntime/tool assembly can otherwise skip the Run executor, terminal
    workers, and service caches.  This node_helper only inspects already-populated
    dependency caches; it never constructs a missing service while cleaning up.  Cleanup is
    best-effort and each failure is logged without masking the original startup exception.
    """

    try:
        if get_conversation_run_executor.cache_info().currsize:
            await get_conversation_run_executor().close()
    except BaseException as exc:
        log.error(
            "lifespan_startup_runtime_cleanup_failed",
            extra={
                "msg": "startup failure 后 Runtime cleanup 失败",
                "data": {"error_type": type(exc).__name__, "error": str(exc)[:500]},
            },
        )

    try:
        if get_terminal_session_service.cache_info().currsize:
            await asyncio.to_thread(get_terminal_session_service().shutdown)
    except BaseException as exc:
        log.error(
            "lifespan_startup_terminal_cleanup_failed",
            extra={
                "msg": "startup failure 后 terminal cleanup 失败",
                "data": {"error_type": type(exc).__name__, "error": str(exc)[:500]},
            },
        )

    try:
        await asyncio.to_thread(close_service_dependencies)
    except BaseException as exc:
        log.error(
            "lifespan_startup_dependency_cleanup_failed",
            extra={
                "msg": "startup failure 后 service dependency/storage cleanup 失败",
                "data": {"error_type": type(exc).__name__, "error": str(exc)[:500]},
            },
        )


@asynccontextmanager
async def _lifespan_impl(_app: FastAPI) -> AsyncIterator[None]:
    """管理 FastAPI 应用生命周期并在关闭时释放运行时资源。

    参数:
        _app: FastAPI 应用实例，仅用于满足 lifespan 协议。

    生成:
        应用运行期间的控制权。

    异常:
        无。Runtime 内部会记录关闭失败。

    副作用:
        按启动顺序安装日志管线、加载配置、初始化服务依赖、收敛遗留 Run、委派与
        最近 Run 的 terminal checkpoint，创建系统级 ``.cosir`` 目录、播种 Hook 注册表、
        装配工具系统与 Agent Runtime，
        并在就绪后写入 ``ready`` 启动状态；关闭时触发 ``SESSION_END``、关闭 Run
        executor 与终端会话、flush 观测数据、关闭服务依赖，最后写入 ``stopped``
        启动状态并卸载日志管线。
    """

    # 在服务器进程内（无论 uvicorn 以 fork 还是 spawn 拉起子进程）尽早配置日志。
    # reload 模式下子进程只执行 lifespan、不会执行 __main__.py；先按固定路径建立文件管线，
    # 确保 Settings.load 或依赖初始化失败也有固定 JSONL 现场。
    install_logging_for_current_process(log_dir=paths.LOG_DIR)
    Settings.load()
    initialize_service_dependencies()
    # Settings.load 可能解析出轮转参数，因此按最终配置重建一次管线。
    install_logging_for_current_process(
        log_dir=paths.LOG_DIR,
        max_bytes=Settings.LOG_MAX_BYTES,
        backup_count=Settings.LOG_BACKUP_COUNT,
    )
    _ensure_system_cosir_dir()
    recovered_runs = get_conversation_run_service().recover_orphaned_runs()
    if recovered_runs:
        log.info(
            "conversation_runs_recovered_after_restart",
            extra={
                "msg": "后端启动时已将遗留 active run 收敛为 cancelled",
                "data": {"run_ids": [run.id for run in recovered_runs]},
            },
        )
    latest_runs = get_conversation_run_service().list_latest_runs()
    get_terminal_session_service().initialize()
    try:
        recovered_terminal_count = await ReactLikeWorkflow().recover_orphaned_terminal_checkpoints(
            latest_runs
        )
        if recovered_terminal_count:
            log.info(
                "orphaned_terminal_sessions_recovered",
                extra={
                    "msg": "后端启动时已扫描并强制关闭最近 Run 的遗留 terminal",
                    "data": {"session_count": recovered_terminal_count},
                },
            )
    except Exception:
        # terminal checkpoint recovery is a startup cleanup side path; it must not prevent the
        # backend from becoming ready when the main database and runtime can still serve requests.
        log.exception(
            "orphaned_terminal_sessions_recovery_failed",
            extra={"msg": "启动期 terminal checkpoint 恢复失败，继续启动 backend", "data": {}},
        )
    # Hook 注册表初始化（启动期单线程播种，必须在 ToolExecutor 首次触发拦截前完成，
    # 否则 HookInterceptor 首次 fire 会拿不到注册表）。无配置层（决策 D3）。
    from app.core.hook import initialize_hook_registry

    initialize_hook_registry()

    # Agent profile 只依赖规范工具名清单，先初始化它即可打破 Agent/ToolRegistry 循环依赖。
    set_agent_registry(build_agent_registry())
    tool_system = ToolSystem.build_tool_system()
    set_tool_system(tool_system)
    set_runtime(
        AgentRuntime(
            process_tool_output_channel_factory=ToolRuntimeOutputChannelFactory(),
        )
    )
    # 当前产品只启动新鲜 ConversationRun；旧 run 不在启动期隐式重放。

    # SESSION_START 挂接：后端进程启动就绪后触发（无消费方拦截，仅作事件接通）。
    # 统一经 HookInterceptor 收口。
    HookInterceptor.safe_fire(HookContext(event=HookEvent.SESSION_START))
    _mark_boot_ready()
    try:
        yield
    finally:
        try:
            # SESSION_END 挂接：进程关闭前触发（服务依赖关闭前，保证日志仍可用）。
            HookInterceptor.safe_fire(HookContext(event=HookEvent.SESSION_END))
            await get_conversation_run_executor().close()
            await asyncio.to_thread(get_terminal_session_service().shutdown)
            await asyncio.to_thread(flush_langfuse)
            await asyncio.to_thread(close_service_dependencies)
            # 模型 HTTP 连接由模型客户端管理，无需进程级显式释放。
            _mark_boot_stopped()
        finally:
            await asyncio.to_thread(shutdown_logging)


def _mark_boot_ready() -> None:
    """在应用装配成功后写入 ``ready`` 启动状态。

    仅在启用了启动状态文件（环境变量 ``CODING_AGENT_BOOT_STATE_FILE``）时生效，
    标记导入与运行时构建已通过，桌面端 supervisor 可据此提前结束等待。

    参数:
        无。

    返回:
        无。

    异常:
        无。

    副作用:
        可能原子写入启动状态文件。
    """
    boot_state_file = boot_state_file_from_env()
    if boot_state_file is not None:
        write_bootstate(boot_state_file, Constant.Boot.READY, step="app_ready")


def _mark_boot_failed(exc: Exception) -> None:
    """在应用生命周期进入 yield 前失败时写入结构化启动错误。

    参数:
        exc: 导致启动失败的异常，用于提取类型名、消息和 traceback。

    返回:
        无。

    异常:
        无。启动状态文件不存在、无法解析、内容不是 JSON 对象或当前阶段已不是
        ``booting`` 时直接跳过：失败上报自身崩溃会掩盖原始启动错误。

    副作用:
        当启动状态文件处于 ``booting`` 阶段时，原子覆盖写入 ``failed`` 状态，
        并带上 ``step="lifespan"``、异常类型、异常消息与完整 traceback。
    """
    boot_state_file = boot_state_file_from_env()
    if boot_state_file is None:
        return
    try:
        current = json.loads(boot_state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        current = {}
    # ``json.loads`` 只保证是合法 JSON，不保证顶层是对象（可能是数组或标量）。
    if not isinstance(current, dict) or current.get("phase") != Constant.Boot.BOOTING:
        return
    write_bootstate(
        boot_state_file,
        Constant.Boot.FAILED,
        step="lifespan",
        error_type=type(exc).__name__,
        error_message=str(exc),
        traceback_text=traceback.format_exc(),
    )


def _mark_boot_stopped() -> None:
    """在应用优雅关闭时写入 ``stopped`` 启动状态。

    仅在启用了启动状态文件（环境变量 ``CODING_AGENT_BOOT_STATE_FILE``）时生效。

    参数:
        无。

    返回:
        无。

    异常:
        无。

    副作用:
        可能原子写入启动状态文件。
    """
    boot_state_file = boot_state_file_from_env()
    if boot_state_file is not None:
        write_bootstate(boot_state_file, Constant.Boot.STOPPED)


def _ensure_system_cosir_dir() -> None:
    """幂等创建系统级 ``.cosir`` 目录，失败降级不阻断启动。

    系统级 ``.cosir`` 用于承载跨 workspace 的系统级配置：**目录名固定为 ``.cosir``**，
    位置由 ``cosir_paths.system_cosir_dir()`` 给出（桌面版为 ``app_data_dir()/.cosir``，
    直接运行时为仓库根 ``.cosir``）。workspace 级 ``.cosir`` 由 ``WorkspaceService``
    在创建工作区时创建，与本函数无关。

    参数:
        无。

    返回:
        无。

    异常:
        无：目录创建失败只记 error 日志，不向上抛出，避免元数据目录不可用阻断后端启动。

    副作用:
        在 ``cosir_paths.system_cosir_dir()`` 创建目录（已存在则幂等跳过）；创建成功
        写 ``system_cosir_initialized`` info 日志，失败写 ``system_cosir_init_failed``
        error 日志。
    """

    cosir_dir = system_cosir_dir()
    try:
        cosir_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error(
            "system_cosir_init_failed",
            extra={
                "msg": "failed to initialize system metadata dir, skipped",
                "data": {
                    "cosir_dir": str(cosir_dir),
                    "error": str(exc),
                    "errno": getattr(exc, "errno", None),
                },
            },
        )
        return
    log.info(
        "system_cosir_initialized",
        extra={
            "msg": "system metadata dir initialized",
            "data": {"cosir_dir": str(cosir_dir)},
        },
    )
