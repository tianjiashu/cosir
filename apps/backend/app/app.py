"""后端 FastAPI 应用装配入口。

本模块只负责应用装配：创建并导出 ``app`` 单例、注册 CORS 与请求/异常日志中间件、
触发各域路由模块级装饰器注册。启动与关闭的生命周期编排（启动顺序、资源释放、
启动状态上报）在 ``app.lifespan`` 中定义，本模块只把 ``lifespan`` 交给 FastAPI。

``app`` 是模块级单例，各域路由文件通过 ``from app.app import app`` 复用同一实例，
因此必须在 ``app`` 定义之后再导入这些模块，否则会产生未初始化引用。

注意：触发域路由注册时**必须**使用 ``importlib.import_module`` 而非
``import app.api.sessions_api`` 这类语句。后者会把顶层包名 ``app`` 绑定到当前模块的全局
命名空间，覆盖掉本模块创建的 FastAPI 实例，导致后续域路由文件通过
``from app.app import app`` 取到的是包模块而非 FastAPI 实例。
"""

import importlib

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.middleware.api_logging import (
    install_http_exception_logging,
    install_request_logging,
)
from app.api.middleware.validation_error import install_request_validation_error_handler
from app.config.logging.logger import log
from app.lifespan import lifespan

app = FastAPI(title="coding-agent backend", lifespan=lifespan)

install_request_logging(app, log)
# CORS 必须最后注册（最外层）：请求日志 middleware 收敛出的 500 才会带上 CORS 头。
# 桌面渲染进程直接连本机动态端口。来源只放本机桌面与开发来源：构建产物的页面来源是 app://cosir
# （见 electron/main/app-protocol.ts），开发期是 Vite 与测试服务；后端只绑定 127.0.0.1，不是网络服务。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["app://cosir"],
    # 开发期来源（Vite、预览、E2E 测试服务）端口不固定，统一按本机地址放行。
    allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=[
        "X-Cosir-Session-Id",
        "X-Cosir-Thread-Id",
        "Accept-Ranges",
        "Content-Range",
        "Content-Length",
        "ETag",
        "X-Cosir-Image-Preview",
    ],
)

install_http_exception_logging(app, log)
# 框架校验错误的响应形状（RequestValidationError 类型的唯一注册者）。
install_request_validation_error_handler(app)

# 触发各域路由的模块级装饰器注册到真实 app 上。
# 这些模块通过 ``from app.app import app`` 复用同一单例，因此必须在本模块
# 已定义 ``app`` 之后再导入，否则会产生未初始化引用。
#
# 必须使用 importlib.import_module 而非 ``import app.api.xxx``：后者会把顶层包名
# ``app`` 绑定到本模块全局命名空间，覆盖此处创建的 FastAPI 实例。
importlib.import_module("app.api.sessions_api")
importlib.import_module("app.api.session_feed_api")
importlib.import_module("app.api.workspaces_api")
importlib.import_module("app.api.workspace_files_api")
importlib.import_module("app.api.configuration.models")
importlib.import_module("app.api.configuration.agents")
importlib.import_module("app.api.configuration.global_instructions")
importlib.import_module("app.api.configuration.main_agent_prompt")
importlib.import_module("app.api.configuration.environment")
importlib.import_module("app.api.configuration.extensions")
importlib.import_module("app.api.configuration.workspace")
importlib.import_module("app.api.terminal_stream_api")
importlib.import_module("app.api.terminal_sessions_api")
importlib.import_module("app.api.host_link_api")
importlib.import_module("app.api.browser_api")
importlib.import_module("app.api.attachments_api")
importlib.import_module("app.api.tools_api")
importlib.import_module("app.assistant_transport.api")
