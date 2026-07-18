"""SQLite task store facade.

This module keeps the public `SQLiteTaskStore` import stable while the concrete
CRUD responsibilities live in focused mixins under `app.storage.crud`.
"""

from pathlib import Path

from app.storage.crud.event_store import EventStoreMixin
from app.storage.crud.step_store import StepStoreMixin
from app.storage.crud.task_store import TaskStoreMixin
from app.storage.crud.turn_store import TurnStoreMixin
from app.storage.crud.workspace_store import WorkspaceStoreMixin
from app.storage.database import create_session_factory
from app.storage.schema import initialize_app_schema


class SQLiteTaskStore(
    WorkspaceStoreMixin,
    TaskStoreMixin,
    TurnStoreMixin,
    StepStoreMixin,
    EventStoreMixin,
):
    """组合工作区、任务、轮次、步骤和事件存储能力。"""

    def __init__(self, database_path: Path) -> None:
        """初始化任务存储并确保 schema 存在。

        参数:
            database_path: SQLite 数据库文件路径。

        返回:
            无。

        异常:
            OSError: 如果数据库目录无法创建。
            sqlalchemy.exc.SQLAlchemyError: 如果 schema 初始化失败。

        副作用:
            创建数据库目录、打开 SQLite engine 并创建主库表。
        """

        self._engine = initialize_app_schema(database_path)
        self._session_factory = create_session_factory(self._engine)

    def close(self) -> None:
        """Dispose the SQLite engine held by this store.

        Parameters:
            None.

        Returns:
            None.

        Raises:
            None.

        Side effects:
            Closes pooled SQLite connections so the database file can be removed on Windows.
        """

        self._engine.dispose()
