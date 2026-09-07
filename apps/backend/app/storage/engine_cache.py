"""SQLAlchemy 引擎与连接池基础设施（进程级缓存）。

单一职责：负责“如何创建一个配置正确的 SQLite 引擎”以及“如何按文件路径缓存 / 复用 /
释放引擎”，是整个 storage 层最底层的引擎工厂与缓存。

本模块为什么存在：
    - SQLite 在多线程 / 连接池场景下需要一批固定 PRAGMA（WAL、busy_timeout、
      foreign_keys）才能稳定工作，这些配置集中在 ``create_sqlite_engine`` 一处，避免各处
      重复且不一致。
    - 同一个数据库文件在整个进程内必须只对应一个 ``Engine``（连接池唯一），否则多个连接
      池并发写同一 SQLite 文件会加剧锁竞争。``EngineCache`` 用“解析后的绝对路径”作为键
      保证单例。

职责边界：
    - 负责：引擎创建、连接池配置、按路径缓存与释放。
    - 不负责：schema 建表 / 迁移（见 ``init_schema``）、三大引擎的编排与生命周期
      （见 ``store_engines``）、任何业务读写（见 ``crud/``）。

对外入口：模块级单例 ``_engine_cache``；上层（``store_engines``）通过它取得引擎，不直接
持有引擎字典。
"""

from pathlib import Path
from threading import Lock

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool


def create_sqlite_engine(database_path: Path) -> Engine:
    """创建项目自有 SQLite 数据库的 SQLAlchemy Engine。

    参数:
        database_path: SQLite 数据库文件路径。

    返回:
        已配置 SQLite PRAGMA 的 SQLAlchemy Engine。

    异常:
        OSError: 如果数据库目录无法创建。
        sqlalchemy.exc.SQLAlchemyError: 如果 Engine 或连接初始化失败。

    副作用:
        创建数据库父目录，并在每个连接上设置 WAL、busy_timeout 与 foreign_keys。
    """

    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{database_path}",
        future=True,
        poolclass=QueuePool,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        # 连接归还连接池时无条件回滚，避免异常路径把未结束事务和写锁带回池中。
        pool_reset_on_return="rollback",
        connect_args={"timeout": 3, "check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        """为每个 SQLite 连接设置项目约定的 PRAGMA。

        参数:
            dbapi_connection: SQLAlchemy 提供的 DB-API 连接。
            _connection_record: SQLAlchemy 连接池记录；当前未使用。

        返回:
            无。

        异常:
            sqlite3.Error: 如果 PRAGMA 执行失败。

        副作用:
            修改连接级 SQLite 设置。
        """

        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=3000")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """创建 SQLAlchemy Session 工厂。

    参数:
        engine: 目标数据库 Engine。

    返回:
        绑定该 Engine 的 Session 工厂。

    异常:
        无。

    副作用:
        无。
    """

    return sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )


class EngineCache:
    """进程级 SQLite 引擎缓存。

    按数据库文件路径缓存已配置的 SQLAlchemy 引擎（含连接池），保证同一路径始终复用
    同一实例，并在进程退出或测试拆卸时统一释放连接池。缓存键使用解析后的绝对路径，
    避免相对路径 / 符号链接导致的重复缓存。
    """

    def __init__(self) -> None:
        """初始化空的引擎缓存。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            创建内部路径→引擎字典与一把保护并发访问的线程锁；此时不创建任何引擎，
            引擎在首次 ``get`` 某路径时才惰性创建。
        """

        self._engines: dict[Path, Engine] = {}
        self._lock = Lock()

    def get(self, database_path: Path) -> Engine:
        """返回指定路径的进程级单例引擎；首次访问时创建并缓存。

        参数:
            database_path: SQLite 数据库文件路径。

        返回:
            已配置连接池的 SQLAlchemy Engine；同一路径始终返回同一实例。

        异常:
            OSError: 如果数据库目录无法创建。
            sqlalchemy.exc.SQLAlchemyError: 如果 Engine 或连接初始化失败。

        副作用:
            首次访问某路径时创建 Engine 并登记到缓存；后续访问复用同一实例。
        """

        resolved = database_path.resolve()
        with self._lock:
            engine = self._engines.get(resolved)
            if engine is None:
                engine = create_sqlite_engine(resolved)
                self._engines[resolved] = engine
            return engine

    def dispose_path(self, database_path: Path) -> None:
        """释放并移除指定路径的引擎及其连接池。

        参数:
            database_path: 之前通过 ``get`` 获取的数据库文件路径。

        返回:
            无。

        异常:
            无。

        副作用:
            关闭该路径 Engine 持有的所有连接，并从缓存中移除对应条目。
        """

        resolved = database_path.resolve()
        with self._lock:
            engine = self._engines.pop(resolved, None)
        if engine is not None:
            engine.dispose()

    def dispose_under(self, directory: Path) -> None:
        """释放并移除位于指定目录下的所有引擎及其连接池。

        参数:
            directory: 待清理的目录；其下任意子路径对应的 Engine 都会被释放。

        返回:
            无。

        异常:
            无。

        副作用:
            关闭匹配 Engine 持有的连接，并从缓存中移除对应条目。
        """

        root = directory.resolve()
        with self._lock:
            targets = [path for path in self._engines if self._is_under(path, root)]
            engines = [self._engines.pop(path) for path in targets]
        for engine in engines:
            engine.dispose()

    def dispose_all(self) -> None:
        """释放缓存中的所有引擎及其连接池。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            关闭所有缓存 Engine 持有的连接并清空缓存；用于进程退出或测试拆卸。
        """

        with self._lock:
            engines = list(self._engines.values())
            self._engines.clear()
        for engine in engines:
            engine.dispose()

    @staticmethod
    def _is_under(path: Path, root: Path) -> bool:
        """判断 path 是否位于 root 目录或其子目录下。

        参数:
            path: 待判断的数据库文件路径。
            root: 基准目录。

        返回:
            当 path 解析后位于 root 之下时返回 True，否则返回 False。

        异常:
            无。

        副作用:
            无。
        """

        try:
            path.resolve().relative_to(root)
        except ValueError:
            return False
        return True


_engine_cache = EngineCache()
