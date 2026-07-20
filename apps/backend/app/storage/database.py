"""SQLAlchemy 数据库基础设施。"""

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import NullPool


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
        poolclass=NullPool,
        connect_args={"timeout": 3},
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

    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
