"""业务 SQLite 引擎统一工厂。

本模块负责主业务库的 SQLAlchemy 引擎生命周期与业务 schema 初始化：引擎由
``engine_cache`` 提供，业务表由 ``initialize_app_schema`` 基于当前 ORM metadata
创建。日志是固定格式的本地文件，不再创建独立日志数据库
或日志 session factory。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import cast

from sqlalchemy import Engine, Table
from sqlalchemy.orm import Session, sessionmaker

from app.storage.engine_cache import _engine_cache, create_session_factory
from app.storage.model.agent_team_run_model import AgentTeamRunModel
from app.storage.model.base import StorageBase
from app.storage.model.conversation_run_model import ConversationRunModel
from app.storage.model.conversation_task_context_model import ConversationTaskContextModel
from app.storage.model.model_config_model import ModelConfigModel
from app.storage.model.task_message_model import TaskMessageModel
from app.storage.model.task_model import TaskModel
from app.storage.model.workspace_model import WorkspaceModel
from app.utils.path import system_cosir as paths

APP_MODELS = (
    TaskMessageModel,
    ModelConfigModel,
    WorkspaceModel,
    TaskModel,
    ConversationRunModel,
    ConversationTaskContextModel,
    AgentTeamRunModel,
)


def initialize_app_schema(engine: Engine) -> None:
    """创建当前业务 schema。"""

    tables = [cast(Table, model.__table__) for model in APP_MODELS]
    StorageBase.metadata.create_all(engine, tables=tables)


@dataclass
class _StorageState:
    """进程级主库引擎状态。"""

    main_engine: Engine | None = None
    main_session_factory: sessionmaker[Session] | None = None
    db_file: Path | None = None


_INIT_LOCK = RLock()
_state = _StorageState()


def _require(value, what: str):
    """返回已初始化的状态值，否则抛出统一的未初始化错误。"""

    if value is None:
        raise RuntimeError(f"storage not initialized; call init_storage() first ({what})")
    return value


def init_storage() -> None:
    """按系统路径创建或复用主库引擎；路径变更时释放旧引擎。"""

    with _INIT_LOCK:
        if _state.main_engine is not None and _state.db_file == paths.DATABASE_FILE:
            return
        if _state.main_engine is not None:
            close_storage()

        _state.db_file = paths.DATABASE_FILE
        _state.main_engine = _engine_cache.get(paths.DATABASE_FILE)
        initialize_app_schema(_state.main_engine)
        _state.main_session_factory = create_session_factory(_state.main_engine)


def main_engine() -> Engine:
    """返回已初始化的主库 SQLAlchemy 引擎。"""

    return _require(_state.main_engine, "main_engine")


def main_session_factory() -> sessionmaker[Session]:
    """返回已初始化的主库 session factory。"""

    return _require(_state.main_session_factory, "main_session_factory")


def close_storage() -> None:
    """释放主库连接池和进程内存储状态。"""

    with _INIT_LOCK:
        if _state.db_file is not None:
            _engine_cache.dispose_path(_state.db_file)
        _state.main_engine = None
        _state.main_session_factory = None
        _state.db_file = None
