"""业务 SQLite 引擎统一工厂。

本模块负责主业务库的 SQLAlchemy 引擎生命周期与业务 schema 初始化：引擎由
``engine_cache`` 提供，业务表由 ``initialize_app_schema`` 基于当前 ORM metadata
一次性建好。日志是固定格式的本地文件，不再创建独立日志数据库或日志 session factory。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import cast

from sqlalchemy import Engine, Table
from sqlalchemy.orm import Session, sessionmaker

from app.storage.engine_cache import _engine_cache, create_session_factory
from app.storage.model.base import StorageBase
from app.storage.model.conversation_run_model import ConversationRunModel
from app.storage.model.conversation_task_context_model import ConversationTaskContextModel
from app.storage.model.model_config_model import ModelConfigModel
from app.storage.model.task_model import TaskModel
from app.storage.model.workspace_model import WorkspaceModel
from app.utils.path import system_cosir as paths

APP_MODELS = (
    ModelConfigModel,
    WorkspaceModel,
    TaskModel,
    ConversationRunModel,
    ConversationTaskContextModel,
)


def initialize_app_schema(engine: Engine) -> None:
    """创建当前应用 metadata 声明的全部业务表。

    参数:
        engine: 已初始化的主库 SQLAlchemy 引擎。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 建表失败。

    副作用:
        在当前数据库创建缺失的应用表。本函数不修改已存在的表，也不执行任何旧
        schema 迁移；遗留旧结构应通过删除本地库重建。
    """

    tables = [cast(Table, model.__table__) for model in APP_MODELS]
    StorageBase.metadata.create_all(engine, tables=tables)

_INIT_LOCK = RLock()


@dataclass
class _StorageState:
    """进程级主库引擎状态。"""

    main_engine: Engine | None = None
    main_session_factory: sessionmaker[Session] | None = None
    db_file: Path | None = None
    checkpoint_file: Path | None = None


_state = _StorageState()


def _require(value, what: str):
    """返回已初始化的状态值，否则抛出统一的未初始化错误。"""

    if value is None:
        raise RuntimeError(f"storage not initialized; call init_storage() first ({what})")
    return value


def init_storage() -> None:
    """初始化主业务 SQLite 引擎和 checkpoint 父目录。

    参数:
        无。路径来自 ``app.config.paths``。

    异常:
        OSError: 数据库或 checkpoint 父目录无法创建。
        sqlalchemy.exc.SQLAlchemyError: 引擎或业务 schema 初始化失败。

    副作用:
        创建或复用主库引擎并初始化业务 schema；路径变化时先释放旧引擎。
    """

    checkpoint_file = _require(paths.CHECKPOINT_FILE, "checkpoint_file")
    with _INIT_LOCK:
        if (
            _state.main_engine is not None
            and _state.db_file == paths.DATABASE_FILE
            and _state.checkpoint_file == checkpoint_file
        ):
            return
        if _state.main_engine is not None:
            close_storage()

        _state.db_file = paths.DATABASE_FILE
        _state.checkpoint_file = checkpoint_file
        _state.main_engine = _engine_cache.get(paths.DATABASE_FILE)
        initialize_app_schema(_state.main_engine)
        _state.main_session_factory = create_session_factory(_state.main_engine)
        checkpoint_file.parent.mkdir(parents=True, exist_ok=True)


def main_engine() -> Engine:
    """返回已初始化的主库 SQLAlchemy 引擎。"""

    return _require(_state.main_engine, "main_engine")


def main_session_factory() -> sessionmaker[Session]:
    """返回已初始化的主库 session factory。"""

    return _require(_state.main_session_factory, "main_session_factory")


def checkpoint_path() -> str:
    """返回 checkpoint SQLite 文件路径。"""

    return str(_require(paths.CHECKPOINT_FILE, "checkpoint_file"))


def close_storage() -> None:
    """释放主库引擎和进程级存储状态。

    checkpoint 数据库由 LangGraph checkpoint 运行时经 aiosqlite 直连，不归本模块释放。
    """

    with _INIT_LOCK:
        if _state.db_file is not None:
            _engine_cache.dispose_path(_state.db_file)
        _state.main_engine = None
        _state.main_session_factory = None
        _state.db_file = None
        _state.checkpoint_file = None
