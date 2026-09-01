"""后端 FastAPI 应用装配入口。

本模块只负责应用装配：创建 ``app`` 单例、定义 ``lifespan``、安装请求日志中间件、
触发各域路由模块级装饰器注册，以及暴露 ``create_app`` 工厂。所有端点逻辑都拆分到
同目录下的域路由文件（``tasks_api`` / ``logs_api`` /

``app`` 是模块级单例，各域路由文件通过 ``from app.api.app import app`` 复用同一实例，
因此必须在 ``app`` 定义之后再导入这些模块，否则会产生未初始化引用。

注意：触发域路由注册时**必须**使用 ``importlib.import_module`` 而非
``import app.api.tasks_api`` 这类语句。后者会把顶层包名 ``app`` 绑定到当前模块的全局
命名空间，覆盖掉本模块在第 48 行创建的 FastAPI 实例，导致后续域路由文件通过
``from app.api.app import app`` 取到的是包模块而非 FastAPI 实例。
"""

import asyncio
import importlib
import json
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.dependencies import (
    build_agent_registry,
    get_conversation_run_executor,
    get_runtime,
    set_agent_registry,
    set_runtime,
    set_tool_system,
)
from app.api.middleware.api_logging import install_http_exception_logging, install_request_logging
from app.api.middleware.transport_error import install_transport_request_error_handler
from app.bootstate import (
    BOOT_PHASE_FAILED,
    BOOT_PHASE_READY,
    BOOT_PHASE_STOPPED,
    boot_state_file_from_env,
    write_bootstate,
)
from app.codegraph import CodeGraphKernelClient, CodeGraphKernelSupervisor
from app.config.logging.configuration import install_logging_for_current_process
from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.observability import flush_langfuse
from app.core.runtime.runner import AgentRuntime
from app.hook import HookContext, HookEvent
from app.hook.hook_interceptor import HookInterceptor
from app.service.depends import (
    close_service_dependencies,
    get_delegation_service,
    initialize_service_dependencies,
)
from app.tools.tool_system import ToolSystem


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """包装生命周期，使 yield 前的启动异常也能写入 bootstate。"""
    try:
        async with _lifespan_impl(_app):
            yield
    except Exception as exc:
        _mark_boot_failed(exc)
        raise


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
        关闭 SQLite 存储等运行时资源（经 ``close_storage``）；Runtime 本身不持有需显式
        释放的资源，故无需对其调用 close。
    """

    # 在服务器进程内（无论 uvicorn 以 fork 还是 spawn 拉起子进程）初始化存储并配置日志。
    # reload 模式下子进程只执行 lifespan、不会执行 __main__.py，因此日志配置必须放在此处，
    # 否则运行期日志既不落文件也不落 SQLite；同时必须先 init_storage 再挂载 SQLite 日志
    # handler，避免 LogStore 因 session 工厂未就绪而抛 RuntimeError 被降级为仅文件日志。
    # 此处重建 handler 也会在 fork 子进程里重新拉起 SQLite 写入线程，规避 fork 后写线程死亡的隐患。
    Settings.load()
    initialize_service_dependencies()
    install_logging_for_current_process(
        log_dir=Settings.LOG_DIR,
        log_database_file=Settings.LOG_DATABASE_FILE,
        sqlite_logging_enabled=Settings.SQLITE_LOGGING_ENABLED,
        queue_size=Settings.LOG_QUEUE_SIZE,
        batch_size=Settings.LOG_BATCH_SIZE,
        flush_interval_ms=Settings.LOG_FLUSH_INTERVAL_MS,
    )
    get_delegation_service().mark_interrupted_delegations_failed("runtime_restarted")

    # 预热常驻 CodeGraph Kernel（应用级预热，对齐「后端启动时预热 Node Kernel」设计）。
    # 启动失败仅降级（CodeGraph 走文件搜索），不阻断后端启动。
    # 必须先于 build_tool_system：workspace_payload 工具装配需要注入已就绪的 Kernel client，
    # 否则 supervisor 未初始化，_codegraph_client() 恒返回 None，工具恒降级（审查暴露）。
    _kernel_supervisor = await _start_codegraph_kernel()

    # Hook 注册表初始化（启动期单线程播种，必须在 ToolScheduler 首次触发拦截前完成，
    # 否则 HookInterceptor 首次 fire 会拿不到注册表）。无配置层（决策 D3）。
    from app.hook.hook_registry import initialize_hook_registry

    initialize_hook_registry()

    tool_system = ToolSystem.build_tool_system(_codegraph_client())
    set_tool_system(tool_system)
    set_agent_registry(build_agent_registry())
    set_runtime(AgentRuntime())
    # 进程重启后由持久化 run/lease 状态恢复未终结的执行；HTTP 订阅不拥有运行生命周期。
    await get_conversation_run_executor().recover(lambda turn: get_runtime().run_turn(turn))

    # SESSION_START 挂接：后端进程启动就绪后触发（无消费方拦截，仅作事件接通）。
    # 统一经 HookInterceptor 收口。
    HookInterceptor.safe_fire(HookContext(event=HookEvent.SESSION_START))
    _mark_boot_ready()
    try:
        yield
    finally:
        # SESSION_END 挂接：进程关闭前触发（服务依赖关闭前，保证日志仍可用）。
        HookInterceptor.safe_fire(HookContext(event=HookEvent.SESSION_END))
        if _kernel_supervisor is not None:
            _kernel_supervisor.shutdown()
        await get_conversation_run_executor().close()
        flush_langfuse()
        close_service_dependencies()
        # 模型 HTTP 连接由 litellm 内部管理，无需进程级显式释放。
        _mark_boot_stopped()


app = FastAPI(title="coding-agent backend", lifespan=lifespan)

install_request_logging(app, log)
install_http_exception_logging(app, log)
install_transport_request_error_handler(app)

# 触发各域路由的模块级装饰器注册到真实 app 上。
# 这些模块通过 ``from app.api.app import app`` 复用同一单例，因此必须在本模块
# 已定义 ``app`` 之后再导入，否则会产生未初始化引用。
#
# 必须使用 importlib.import_module 而非 ``import app.api.xxx``：后者会把顶层包名
# ``app`` 绑定到本模块全局命名空间，覆盖此处创建的 FastAPI 实例。
importlib.import_module("app.api.tasks_api")
importlib.import_module("app.api.workspaces_api")
importlib.import_module("app.api.changes_api")
importlib.import_module("app.api.logs_api")
importlib.import_module("app.api.providers_api")
importlib.import_module("app.api.models_api")
importlib.import_module("app.api.assistant_api")


async def _start_codegraph_kernel() -> CodeGraphKernelSupervisor | None:
    """启动常驻 CodeGraph Kernel 子进程并设进程级单例；失败降级不阻断启动。

    应用级预热：在应用装配完成后、标记 boot ready 前，拉起 CodeGraph Kernel 常驻
    子进程（``supervisor.start()``），避免首次 Agent 工具调用才发现 Kernel 不可用。

    启动失败不阻断后端启动：异常仅记录日志，supervisor 仍通过 ``set_kernel_supervisor``
    设为进程级单例（state 为 failed），使 ``get_client()`` 抛 ``CodeGraphKernelUnavailableError``，
    由 service 层（如 ``CodeGraphIndexPrepareHook`` 内置 Hook 的 ensure_ready 降级分支）
    降级到文件搜索。

    已知延迟：``supervisor.start()`` 内含握手（``client.hello``），极端场景（node 挂起
    无响应）下可能阻塞最多一个 RPC 超时（默认 30s），从而延迟 ``_mark_boot_ready()``。
    这是「延迟 ready」而非「不 ready」——握手失败会走降级不抛。正常场景握手秒级完成。

    参数:
        无。

    返回:
        已装配的 ``CodeGraphKernelSupervisor`` 单例；启动成功则 state=ready。
        （当前实现始终返回非 None，因失败也保留 supervisor 供状态查询；预留 None 分支
        供测试注入或未来「完全禁用 Kernel」配置。）

    异常:
        无（启动失败归一化为降级，不向上抛）。

    副作用:
        创建 Kernel 子进程并设进程级 supervisor 单例；失败时记录日志并保留 failed 状态。
    """

    from app.codegraph import CodeGraphKernelSupervisor, set_kernel_supervisor

    supervisor = CodeGraphKernelSupervisor()
    try:
        # supervisor.start() 是同步阻塞（spawn + 握手），放进线程池避免卡事件循环。
        await asyncio.to_thread(supervisor.start)
    except Exception:
        log.exception(
            "codegraph_kernel_startup_failed",
            extra={"msg": "CodeGraph Kernel 启动失败，降级到文件搜索"},
        )
    set_kernel_supervisor(supervisor)
    return supervisor


def _codegraph_client() -> CodeGraphKernelClient | None:
    """安全取得 CodeGraph Kernel RPC 客户端；Kernel 未就绪返回 None。

    参数:
        无。

    返回:
        Kernel 就绪时的 ``CodeGraphKernelClient``；supervisor 未初始化或 Kernel 非
        ready 时返回 None（CodeGraph 工具仍注册，execute 降级）。

    异常:
        无（内部捕获，不向上抛）。

    副作用:
        无。
    """

    from app.codegraph import CodeGraphKernelUnavailableError, get_kernel_supervisor

    try:
        return get_kernel_supervisor().get_client()
    except (RuntimeError, CodeGraphKernelUnavailableError):
        return None


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
        write_bootstate(boot_state_file, BOOT_PHASE_READY, step="app_ready")


def _mark_boot_failed(exc: Exception) -> None:
    """在应用生命周期进入 yield 前失败时写入结构化启动错误。"""
    boot_state_file = boot_state_file_from_env()
    if boot_state_file is None:
        return
    try:
        current = json.loads(boot_state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        current = {}
    if current.get("phase") != "booting":
        return
    write_bootstate(
        boot_state_file,
        BOOT_PHASE_FAILED,
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
        write_bootstate(boot_state_file, BOOT_PHASE_STOPPED)
