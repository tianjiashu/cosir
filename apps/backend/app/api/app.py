"""后端 FastAPI 应用装配入口。

本模块只负责应用装配：创建 ``app`` 单例、定义 ``lifespan``、安装请求日志中间件、
触发各域路由模块级装饰器注册，以及暴露 ``create_app`` 工厂。所有端点逻辑都拆分到
同目录下的域路由文件（``tasks_api`` / ``logs_api`` / ``traces_api`` /

``app`` 是模块级单例，各域路由文件通过 ``from app.api.app import app`` 复用同一实例，
因此必须在 ``app`` 定义之后再导入这些模块，否则会产生未初始化引用。

注意：触发域路由注册时**必须**使用 ``importlib.import_module`` 而非
``import app.api.tasks_api`` 这类语句。后者会把顶层包名 ``app`` 绑定到当前模块的全局
命名空间，覆盖掉本模块在第 48 行创建的 FastAPI 实例，导致后续域路由文件通过
``from app.api.app import app`` 取到的是包模块而非 FastAPI 实例。
"""

import importlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.depends.dependencies import (
    build_agent_registry,
    build_runtime,
    set_agent_registry,
    set_runtime,
    set_tool_system,
)
from app.api.middleware.api_logging import install_http_exception_logging, install_request_logging
from app.bootstate import (
    BOOT_PHASE_READY,
    BOOT_PHASE_STOPPED,
    boot_state_file_from_env,
    write_bootstate,
)
from app.config.settings import default_settings
from app.storage.store_engines import close_storage
from app.tools.tool_system import ToolSystem


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """管理 FastAPI 应用生命周期并在关闭时释放运行时资源。

    参数:
        _app: FastAPI 应用实例，仅用于满足 lifespan 协议。

    生成:
        应用运行期间的控制权。

    异常:
        无。Runtime 内部会记录关闭失败。

    副作用:
        关闭 Runtime 持有的运行时资源。
    """

    runtime = getattr(_app.state, "runtime_override", None)
    tool_system = getattr(_app.state, "tool_system_override", None)

    if runtime is None:
        settings = default_settings()
        tool_system = tool_system or ToolSystem.build_tool_system(settings)
        set_tool_system(tool_system)
        set_agent_registry(build_agent_registry())
        runtime = build_runtime(tool_system=tool_system, settings=settings)
        set_runtime(runtime)
    else:
        if tool_system is not None:
            set_tool_system(tool_system)
        # 覆写路径：从 runtime 取出其持有的 registry 同步到进程级单例，
        # 保证 GET /agents 与执行引擎共享同一份目录。
        set_agent_registry(runtime.agent_registry)
        set_runtime(runtime)

    _mark_boot_ready()
    try:
        yield
    finally:
        runtime.close()
        close_storage()
        _mark_boot_stopped()


app = FastAPI(title="coding-agent backend", lifespan=lifespan)

install_request_logging(app, logging.getLogger("coding_agent.backend"))
install_http_exception_logging(app, logging.getLogger("coding_agent.backend"))


# 触发各域路由的模块级装饰器注册到真实 app 上。
# 这些模块通过 ``from app.api.app import app`` 复用同一单例，因此必须在本模块
# 已定义 ``app`` 之后再导入，否则会产生未初始化引用。
#
# 必须使用 importlib.import_module 而非 ``import app.api.xxx``：后者会把顶层包名
# ``app`` 绑定到本模块全局命名空间，覆盖此处创建的 FastAPI 实例。
importlib.import_module("app.api.tasks_api")
importlib.import_module("app.api.workspaces_api")
importlib.import_module("app.api.turns_api")
importlib.import_module("app.api.logs_api")
importlib.import_module("app.api.traces_api")
importlib.import_module("app.api.agents_api")


def create_app(runtime=None, tool_system=None) -> FastAPI:
    """Return the module-level FastAPI app and store optional overrides.

    参数:
        runtime: 可选的运行时依赖。省略时会构建默认的本地运行时。
        tool_system: 可选的工具系统。省略且 runtime 为空时，会在应用装配阶段构建。

    返回:
        模块级 FastAPI 应用单例。

    异常:
        RuntimeError: 当当前环境未安装 FastAPI 或运行时构建失败时抛出。

    副作用:
        构建（或接收）运行时并写入模块级单例，供依赖注入使用。
    """

    app.state.runtime_override = runtime
    app.state.tool_system_override = tool_system
    if runtime is not None:
        set_agent_registry(runtime.agent_registry)
        set_runtime(runtime)
    if tool_system is not None:
        set_tool_system(tool_system)
    return app


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
