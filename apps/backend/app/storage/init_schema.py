"""SQLite schema 初始化。

数据库以当前 SQLAlchemy metadata 为准；仅保留 fork 所需的一个窄范围 schema 正规化，
用于移除本机开发库遗留的 checkpoint 全局唯一约束。
"""

from typing import cast

from sqlalchemy import Engine, Table, inspect

from app.storage.model.base import StorageBase
from app.storage.model.conversation_command_model import ConversationCommandModel
from app.storage.model.conversation_run_model import ConversationRunModel
from app.storage.model.conversation_task_context_model import ConversationTaskContextModel
from app.storage.model.conversation_task_snapshot_model import ConversationTaskSnapshotModel
from app.storage.model.delegation_model import DelegationModel
from app.storage.model.file_snapshot_model import FileSnapshotModel
from app.storage.model.log_model import LogEntryModel
from app.storage.model.model_entry_model import ModelEntryModel
from app.storage.model.provider_model import ProviderModel
from app.storage.model.task_model import TaskModel
from app.storage.model.terminal_session_model import TerminalSessionModel
from app.storage.model.workspace_model import WorkspaceModel

APP_MODELS = (
    ProviderModel,
    ModelEntryModel,
    WorkspaceModel,
    TaskModel,
    ConversationRunModel,
    ConversationCommandModel,
    ConversationTaskSnapshotModel,
    ConversationTaskContextModel,
    FileSnapshotModel,
    DelegationModel,
    TerminalSessionModel,
)
LOG_MODELS = (LogEntryModel,)


def initialize_app_schema(engine: Engine) -> None:
    """创建当前应用 metadata 声明的全部业务表。

    参数:
        engine: 已初始化的主库 SQLAlchemy 引擎。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 建表失败。

    副作用:
        在当前数据库创建缺失的应用表，并清理当前版本仍可能存在的
        ``conversation_runs.checkpoint_thread_id`` 全局唯一约束。
    """

    tables = [cast(Table, model.__table__) for model in APP_MODELS]
    StorageBase.metadata.create_all(engine, tables=tables)
    _ensure_tasks_sqlite_autoincrement(engine)
    _remove_legacy_checkpoint_unique_constraint(engine)


def _quote_sqlite_identifier(identifier: str) -> str:
    """引用一个由 schema 元数据提供的 SQLite 标识符。"""

    return '"' + identifier.replace('"', '""') + '"'


def _ensure_tasks_sqlite_autoincrement(engine: Engine) -> None:
    """为历史 SQLite ``tasks`` 表补上不可复用的主键语义。

    参数:
        engine: 已初始化的主库 SQLAlchemy 引擎。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: SQLite 表重建或数据复制失败时抛出，事务由
            SQLAlchemy 回滚。

    副作用:
        如果现有 ``tasks`` 表未声明 ``AUTOINCREMENT``，在本地写事务中按当前 ORM
        metadata 重建该表并原样复制数据。该迁移不删除业务行；显式复制原主键会同步
        SQLite 的 ``sqlite_sequence``，保证后续新任务不会复用已存在的最大 ID。
    """

    if engine.dialect.name != "sqlite":
        return

    table_name = TaskModel.__tablename__
    with engine.connect() as connection:
        table_sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).scalar_one_or_none()
        if not isinstance(table_sql, str) or "AUTOINCREMENT" in table_sql.upper():
            return

        legacy_table_name = f"{table_name}__legacy_autoincrement"
        table = cast(Table, TaskModel.__table__)
        columns = ", ".join(_quote_sqlite_identifier(column.name) for column in table.columns)
        quoted_table = _quote_sqlite_identifier(table_name)
        quoted_legacy_table = _quote_sqlite_identifier(legacy_table_name)

        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        connection.commit()
        try:
            with connection.begin():
                for index in inspect(connection).get_indexes(table_name):
                    index_name = index.get("name")
                    if index_name:
                        connection.exec_driver_sql(
                            f"DROP INDEX IF EXISTS {_quote_sqlite_identifier(index_name)}"
                        )
                connection.exec_driver_sql(
                    f"ALTER TABLE {quoted_table} RENAME TO {quoted_legacy_table}"
                )
                table.create(bind=connection, checkfirst=False)
                connection.exec_driver_sql(
                    f"INSERT INTO {quoted_table} ({columns}) "  # noqa: S608 - identifiers come from internal SQLAlchemy metadata
                    f"SELECT {columns} FROM {quoted_legacy_table}"
                )
                connection.exec_driver_sql(f"DROP TABLE {quoted_legacy_table}")
                for index in table.indexes:
                    index.create(bind=connection, checkfirst=True)
        finally:
            connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()


def _remove_legacy_checkpoint_unique_constraint(engine: Engine) -> None:
    """重建仍带旧 checkpoint 全局唯一约束的 SQLite 表。

    ``create_all`` 不会修改已存在的表，而历史开发库曾把
    ``checkpoint_thread_id`` 声明成全局唯一；Fork 需要多个 cloned Run 共享该引用，
    因此只在检测到这条旧约束时重建表，保留现有数据和当前模型索引。
    """

    if engine.dialect.name != "sqlite":
        return

    table_name = ConversationRunModel.__tablename__
    unique_constraints = inspect(engine).get_unique_constraints(table_name)
    if not any(
        constraint.get("column_names") == ["checkpoint_thread_id"]
        for constraint in unique_constraints
    ):
        return

    legacy_table_name = f"{table_name}__legacy_checkpoint_unique"
    table = cast(Table, ConversationRunModel.__table__)
    columns = ", ".join(_quote_sqlite_identifier(column.name) for column in table.columns)
    quoted_table = _quote_sqlite_identifier(table_name)
    quoted_legacy_table = _quote_sqlite_identifier(legacy_table_name)

    with engine.connect() as connection:
        # SQLite 迁移需要暂时允许重建被其他业务表引用的表；整个重建仍在
        # 一个本地写事务中完成，提交前不会对外可见。
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        # 新版 SQLite 的 ALTER TABLE 默认会把其他表的 FK 指向重命名后的
        # legacy 表；开启 legacy_alter_table 才能让它们继续指向重建后的新表。
        connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        connection.commit()
        try:
            with connection.begin():
                for index in inspect(connection).get_indexes(table_name):
                    index_name = index.get("name")
                    if index_name:
                        connection.exec_driver_sql(
                            f"DROP INDEX IF EXISTS {_quote_sqlite_identifier(index_name)}"
                        )
                connection.exec_driver_sql(
                    f"ALTER TABLE {quoted_table} RENAME TO {quoted_legacy_table}"
                )
                table.create(bind=connection, checkfirst=False)
                connection.exec_driver_sql(
                    f"INSERT INTO {quoted_table} ({columns}) "  # noqa: S608 - identifiers come from internal SQLAlchemy metadata
                    f"SELECT {columns} FROM {quoted_legacy_table}"
                )
                connection.exec_driver_sql(f"DROP TABLE {quoted_legacy_table}")
                for index in table.indexes:
                    index.create(bind=connection, checkfirst=True)
        finally:
            connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()


def initialize_log_schema(engine: Engine) -> None:
    """创建日志库当前 metadata 声明的表。"""

    for model in LOG_MODELS:
        table = cast(Table, model.__table__)
        table.create(bind=engine, checkfirst=True)
        for index in table.indexes:
            index.create(bind=engine, checkfirst=True)
