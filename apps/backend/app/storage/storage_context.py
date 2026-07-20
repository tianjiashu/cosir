"""Application-wide storage context.

单一职责：统一管理主库 SQLite engine、schema 初始化和 session factory，
避免每个 CRUD 类各自创建 engine。

用法::

    ctx = StorageContext(settings.database_file)
    task_store = SQLiteTaskStore(ctx.session_factory)
    run_store = DurableRunStore(ctx.session_factory)
    ctx.close()
"""

from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.storage.database import create_session_factory, create_sqlite_engine
from app.storage.schema import initialize_app_schema


class StorageContext:
    """Holds the application SQLite engine and session factory.

    Can be used directly as a session factory::

        with ctx() as session:
            session.get(...)
    """

    def __init__(self, database_path: Path) -> None:
        """Initialize engine, schema, and session factory.

        Args:
            database_path: Path to the application SQLite database file.
        """

        self.database_path = database_path
        self._engine = create_sqlite_engine(database_path)
        initialize_app_schema(self._engine)
        self.session_factory: sessionmaker = create_session_factory(self._engine)

    def __call__(self) -> Session:
        """Return a new Session from the underlying factory."""
        return self.session_factory()

    def close(self) -> None:
        """Dispose the SQLite engine."""
        self._engine.dispose()
