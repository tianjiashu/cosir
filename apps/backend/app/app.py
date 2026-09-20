"""后端 FastAPI 应用装配入口。

本模块只负责应用装配：创建并导出 ``app`` 单例、注册 CORS 与请求/异常日志中间件、
触发各域路由模块级装饰器注册。启动与关闭的生命周期编排（启动顺序、资源释放、
启动状态上报）在 ``app.lifespan`` 中定义，本模块只把 ``lifespan`` 交给 FastAPI。

``app`` 是模块级单例，各域路由文件通过 ``from app.app import app`` 复用同一实例，
因此必须在 ``app`` 定义之后再导入这些模块，否则会产生未初始化引用。

注意：触发域路由注册时**必须**使用 ``importlib.import_module`` 而非
``import app.api.tasks_api`` 这类语句。后者会把顶层包名 ``app`` 绑定到当前模块的全局
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
from app.api.middleware.transport_error import install_transport_request_error_handler
from app.config.logging.logger import log
from app.lifespan import lifespan

app = FastAPI(title="coding-agent backend", lifespan=lifespan)

# Desktop WebView connects directly to the dynamically allocated loopback port.
# Keep this limited to local desktop/dev origins; the backend itself remains
# bound to 127.0.0.1 and is never exposed as a network service.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://127.0.0.1:4173",
        "http://localhost:4173",
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
    ],
    allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Cosir-Task-Id", "X-Cosir-Thread-Id"],
)

install_request_logging(app, log)
install_http_exception_logging(app, log)
install_transport_request_error_handler(app)

# 触发各域路由的模块级装饰器注册到真实 app 上。
# 这些模块通过 ``from app.app import app`` 复用同一单例，因此必须在本模块
# 已定义 ``app`` 之后再导入，否则会产生未初始化引用。
#
# 必须使用 importlib.import_module 而非 ``import app.api.xxx``：后者会把顶层包名
# ``app`` 绑定到本模块全局命名空间，覆盖此处创建的 FastAPI 实例。
importlib.import_module("app.api.tasks_api")
importlib.import_module("app.api.workspaces_api")
importlib.import_module("app.api.providers_api")
importlib.import_module("app.api.models_api")
importlib.import_module("app.api.terminal_api")
importlib.import_module("app.api.attachments_api")
importlib.import_module("app.assistant_transport.assistant_api")
