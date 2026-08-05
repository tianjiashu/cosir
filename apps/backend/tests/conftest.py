"""pytest 共享 fixture 定义。

单一职责：只提供跨测试文件复用的测试夹具，不承载任何业务断言或被测逻辑。
``isolated_storage`` 原先在多个变更集相关测试文件中各有一份副本，此处收口为
唯一定义，避免多份夹具行为漂移；``api_client`` 提供不触发 lifespan 的
FastAPI TestClient，供 API 层测试直连端点函数。
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.app import app
from app.config.settings import Settings
from app.service.depends import reset_service_dependencies
from app.storage.store_engines import close_storage, init_storage


@pytest.fixture
def isolated_storage(tmp_path: Path) -> Iterator[dict[str, Any]]:
    """为每个测试搭建独立的 storage（主库 + checkpoint + 日志库）。

    用例之间不共享任何库文件：主库、LangGraph checkpoint 与日志库全部落在该用例的
    ``tmp_path`` 下，用完即销毁，从而避免跨用例的数据污染与 schema 残留。

    参数:
        tmp_path: pytest 内置夹具提供的用例级独立临时目录。

    返回:
        生成器夹具，产出含 ``tmp_path`` 键的字典，供用例拼接 workspace 根路径。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果 ``init_storage`` 建表失败。

    副作用:
        覆盖 ``Settings`` 的三个库路径静态属性；创建并初始化临时库文件；用例结束后
        关闭 storage 引擎并重置 service 层依赖缓存。
    """
    Settings.override(
        DATABASE_FILE=tmp_path / "app.sqlite3",
        CHECKPOINT_FILE=tmp_path / "ckpt.sqlite",
        LOG_DATABASE_FILE=tmp_path / "logs.sqlite3",
    )
    init_storage()
    reset_service_dependencies()
    yield {"tmp_path": tmp_path}
    close_storage()
    reset_service_dependencies()


@pytest.fixture
def api_client(isolated_storage: dict[str, Any]) -> TestClient:
    """提供指向模块级 FastAPI 单例的测试客户端。

    依赖 ``isolated_storage`` 以复用同一临时库上下文：端点函数在测试进程内
    直连 service 层（lru_cache 单例），能读到该用例隔离的库数据。

    注意：这里**不**使用 ``with TestClient(app)`` 进入 lifespan，避免触发
    runtime / tool_system 构建等与端点无关的装配；``isolated_storage`` 已完成
    storage 初始化并重置 service 依赖缓存。

    参数:
        isolated_storage: 用例级隔离存储夹具。

    返回:
        ``TestClient`` 实例，用于直接调用已注册的 HTTP 端点。

    异常:
        无。

    副作用:
        无。
    """
    return TestClient(app)
