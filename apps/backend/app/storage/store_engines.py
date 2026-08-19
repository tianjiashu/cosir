"""SQLAlchemy 引擎统一工厂。

单一职责：集中创建、缓存、释放两个业务所需的 SQLAlchemy 同步引擎（主库 / 日志库），
路径全部来自 ``Settings`` 类级静态属性（``Settings.LOG_DIR`` / ``Settings.DATABASE_FILE`` 等）；
CRUD 不再接收引擎或路径参数，统一通过本模块的访问器取得 session 工厂。
LangGraph checkpoint 由 ``app.core.runtime.runs.checkpointer`` 经
aiosqlite 直连 ``Settings.CHECKPOINT_FILE``，不经过本模块引擎。

职责边界：
    - 负责：两个 SQLAlchemy 引擎（主库 / 日志库）的按需创建、进程级缓存复用
      （委托 ``engine_cache``）、schema 初始化触发（委托 ``init_schema``）、统一释放。
    - 不负责：引擎底层 PRAGMA 与连接池细节（见 ``engine_cache``）、建表与迁移 SQL
      （见 ``init_schema``）、任何业务读写（见 ``crud/``）。

生命周期约定：进程启动时调用一次 ``init_storage()``；各 CRUD 在其 ``__init__``
里通过访问器（如 ``main_session_factory()``）取得 session 工厂或引擎，因此必须在
``init_storage`` 之后构造；进程退出或测试拆卸时调用 ``close_storage()`` 释放全部连接池。
未初始化即调用访问器会抛出统一的 ``RuntimeError``。

用法::

    from app.storage.store_engines import init_storage, main_session_factory, close_storage

    init_storage()
    task_crud = TaskCrud()          # 内部调用 main_session_factory()
    ...
    close_storage()                 # 进程退出或测试拆卸时释放
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.storage.engine_cache import _engine_cache, create_session_factory
from app.storage.init_schema import initialize_app_schema, initialize_log_schema

_INIT_LOCK = RLock()


@dataclass
class _StorageState:
    """进程级存储引擎状态。

    由 ``init_storage`` 填充、``close_storage`` 清空；所有访问器通过 ``_require`` 在状态
    缺失时抛出一致的未初始化错误，避免散落的模块级全局变量与 ``global`` 声明。
    """

    main_engine: Engine | None = None
    main_session_factory: sessionmaker[Session] | None = None
    log_engine: Engine | None = None
    log_session_factory: sessionmaker[Session] | None = None
    db_file: Path | None = None
    log_db_file: Path | None = None
    checkpoint_file: Path | None = None


_state = _StorageState()


def _require(value, what: str):
    """返回已初始化的存储状态值，未初始化则抛出一致的 RuntimeError。

    参数:
        value: 待返回的存储状态值（可能为 None 表示未初始化）。
        what: 状态项名称，用于错误提示。

    返回:
        非 None 的存储状态值。

    异常:
        RuntimeError: 如果 value 为 None（``init_storage`` 尚未调用）。

    副作用:
        无。
    """

    if value is None:
        raise RuntimeError(f"storage not initialized; call init_storage() first ({what})")
    return value


def init_storage() -> None:
    """按 ``Settings`` 类级静态配置初始化并缓存全部引擎与 session 工厂（幂等）。

    参数:
        无。后端路径类配置由 ``app.config.settings.Settings`` 的类级静态属性提供。

    返回:
        无。

    异常:
        OSError: 如果数据库父目录无法创建。
        sqlalchemy.exc.SQLAlchemyError: 如果引擎或 schema 初始化失败。

    副作用:
        首次调用时创建主库、日志库两个引擎并初始化 schema；checkpoint 数据库父目录一并
        预创建（供 ``app.core.runtime.runs.checkpointer`` 经 aiosqlite 直连）；
        已初始化且路径配置不同则先 ``close_storage`` 再重建。
    """

    log_database_file = _require(Settings.LOG_DATABASE_FILE, "log_database_file")
    checkpoint_file = _require(Settings.CHECKPOINT_FILE, "checkpoint_file")
    with _INIT_LOCK:
        if (
            _state.main_engine is not None
            and _state.db_file == Settings.DATABASE_FILE
            and _state.log_db_file == log_database_file
            and _state.checkpoint_file == checkpoint_file
        ):
            return
        if _state.main_engine is not None:
            close_storage()
        _state.db_file = Settings.DATABASE_FILE
        _state.log_db_file = log_database_file
        _state.checkpoint_file = checkpoint_file

        _state.main_engine = _engine_cache.get(Settings.DATABASE_FILE)
        initialize_app_schema(_state.main_engine)
        _state.main_session_factory = create_session_factory(_state.main_engine)

        _state.log_engine = _engine_cache.get(log_database_file)
        initialize_log_schema(_state.log_engine)
        _state.log_session_factory = create_session_factory(_state.log_engine)

        checkpoint_file.parent.mkdir(parents=True, exist_ok=True)


def main_engine() -> Engine:
    """返回主库引擎（进程级单例）。

    参数:
        无。

    返回:
        主库 SQLAlchemy 引擎。

    异常:
        RuntimeError: 如果 ``init_storage`` 尚未调用。

    副作用:
        无。
    """
    return _require(_state.main_engine, "main_engine")


def main_session_factory() -> sessionmaker[Session]:
    """返回主库 session 工厂（进程级单例）。

    参数:
        无。

    返回:
        主库 session 工厂。

    异常:
        RuntimeError: 如果 ``init_storage`` 尚未调用。

    副作用:
        无。
    """

    return _require(_state.main_session_factory, "main_session_factory")


def log_engine() -> Engine:
    """返回日志库引擎（进程级单例）。

    参数:
        无。

    返回:
        日志库 SQLAlchemy 引擎。

    异常:
        RuntimeError: 如果 ``init_storage`` 尚未调用。

    副作用:
        无。
    """

    return _require(_state.log_engine, "log_engine")


def log_session_factory() -> sessionmaker[Session]:
    """返回日志库 session 工厂（进程级单例）。

    参数:
        无。

    返回:
        日志库 session 工厂。

    异常:
        RuntimeError: 如果 ``init_storage`` 尚未调用。

    副作用:
        无。
    """

    return _require(_state.log_session_factory, "log_session_factory")


def checkpoint_path() -> str:
    """返回 checkpoint sqlite 文件路径字符串（来自 ``Settings.CHECKPOINT_FILE``）。

    参数:
        无。

    返回:
        checkpoint 数据库文件绝对路径字符串。

    异常:
        RuntimeError: 如果 ``init_storage`` 尚未调用。

    副作用:
        无。
    """

    return str(_require(Settings.CHECKPOINT_FILE, "checkpoint_file"))


def close_storage() -> None:
    """释放全部引擎与连接池并清空缓存。

    参数:
        无。

    返回:
        无。

    异常:
        无。

    副作用:
        关闭主库、日志库两个 SQLAlchemy 引擎持有的连接并清空进程级缓存；
        checkpoint 数据库由 ``app.core.runtime.runs.checkpointer`` 经 aiosqlite
        直连、不归本模块释放；用于进程退出或测试拆卸。
    """

    with _INIT_LOCK:
        if _state.main_engine is not None and Settings.DATABASE_FILE is not None:
            _engine_cache.dispose_path(Settings.DATABASE_FILE)
        if _state.log_engine is not None and Settings.LOG_DATABASE_FILE is not None:
            _engine_cache.dispose_path(Settings.LOG_DATABASE_FILE)
        _state.main_engine = None
        _state.main_session_factory = None
        _state.log_engine = None
        _state.log_session_factory = None
        _state.db_file = None
        _state.log_db_file = None
        _state.checkpoint_file = None
