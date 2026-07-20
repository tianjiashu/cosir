"""SQLAlchemy schema initialization."""

import sys

from sqlalchemy import Engine, inspect, text

from app.storage.model.durable_model import DurableRunModel
from app.storage.model.log_model import LogEntryModel
from app.storage.model.task_model import TaskModel
from app.storage.model.trace_model import TraceEventModel, TraceSpanModel
from app.storage.model.turn_model import TurnModel
from app.storage.model.workspace_model import WorkspaceModel

APP_MODELS = (
    WorkspaceModel,
    TaskModel,
    TurnModel,
    DurableRunModel,
    TraceEventModel,
    TraceSpanModel,
)
LOG_MODELS = (LogEntryModel,)
LOG_SCHEMA_VERSION = 2


def initialize_app_schema(engine: Engine) -> None:
    """Initialize the main application SQLite schema.

    Parameters:
        engine: Initialized SQLAlchemy engine (from ``create_sqlite_engine``).

    Raises:
        sqlalchemy.exc.SQLAlchemyError: If table creation or migration fails.

    Side effects:
        Creates missing application tables/columns.
    """

    with engine.begin() as connection:
        for model in APP_MODELS:
            model.__table__.create(bind=connection, checkfirst=True)
        _ensure_model_columns(connection, engine)


def _default_literal_for_type(column_type) -> str:
    """Return a SQLite literal default for a missing NOT NULL column.

    Parameters:
        column_type: SQLAlchemy column type.

    Returns:
        SQL literal suitable for an ``ALTER TABLE`` default.

    Raises:
        None.

    Side effects:
        None.
    """

    type_name = str(column_type).upper()
    if "INT" in type_name or "BOOLEAN" in type_name:
        return "0"
    if "FLOAT" in type_name or "REAL" in type_name or "NUMERIC" in type_name:
        return "0.0"
    return "''"


def _ensure_model_columns(connection, engine) -> None:
    """Add missing model columns to existing application tables.

    Parameters:
        connection: Active SQLAlchemy connection.
        engine: SQLAlchemy engine used for dialect compilation.

    Returns:
        None.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: If migration fails.

    Side effects:
        May alter existing tables by adding missing columns.
    """

    inspector = inspect(connection)
    for model in APP_MODELS:
        table = model.__table__
        if not inspector.has_table(table.name):
            continue
        existing = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            ddl_type = column.type.compile(dialect=engine.dialect)
            if column.nullable:
                column_ddl = f"{column.name} {ddl_type}"
            elif column.server_default is not None:
                column_ddl = (
                    f"{column.name} {ddl_type} NOT NULL DEFAULT {column.server_default.arg}"
                )
            else:
                default = _default_literal_for_type(column.type)
                column_ddl = f"{column.name} {ddl_type} NOT NULL DEFAULT {default}"
            connection.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column_ddl}"))
            sys.stderr.write(f"[storage.schema] added missing column {table.name}.{column.name}\n")


def initialize_log_schema(engine: Engine) -> None:
    """Initialize the log SQLite schema.

    Parameters:
        engine: Initialized SQLAlchemy engine (from ``create_sqlite_engine``).

    Raises:
        sqlalchemy.exc.SQLAlchemyError: If table creation fails.

    Side effects:
        Creates or rebuilds the log table schema.
    """

    with engine.begin() as connection:
        current_version = int(connection.execute(text("PRAGMA user_version")).scalar_one())
        if current_version == 0 and _has_table(connection, "log_entries"):
            _rebuild_log_schema(connection, current_version, LOG_SCHEMA_VERSION)
        elif current_version == 0:
            _create_log_schema(connection)
            connection.execute(text(f"PRAGMA user_version = {LOG_SCHEMA_VERSION}"))
        elif current_version < LOG_SCHEMA_VERSION:
            _rebuild_log_schema(connection, current_version, LOG_SCHEMA_VERSION)


def _has_table(connection, table_name: str) -> bool:
    """Return whether a table exists in the current connection."""

    return inspect(connection).has_table(table_name)


def _create_log_schema(connection) -> None:
    """Create log schema tables and indexes."""

    for model in LOG_MODELS:
        model.__table__.create(bind=connection, checkfirst=True)
        for index in model.__table__.indexes:
            index.create(bind=connection, checkfirst=True)


def _rebuild_log_schema(connection, current_version: int, target_version: int) -> None:
    """Rebuild the log schema."""

    for model in LOG_MODELS:
        model.__table__.drop(bind=connection, checkfirst=True)
    _create_log_schema(connection)
    connection.execute(text(f"PRAGMA user_version = {target_version}"))
    sys.stderr.write(
        "[storage.schema] log schema rebuilt; "
        f"old_version={current_version}, new_version={target_version}\n"
    )
