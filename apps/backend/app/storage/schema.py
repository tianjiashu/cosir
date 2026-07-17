"""SQLAlchemy schema 初始化入口。"""

import sys
from pathlib import Path

from sqlalchemy import Engine, inspect, text

from app.storage.database import create_sqlite_engine
from app.storage.model.approval import ApprovalDecisionModel, ApprovalRequestModel
from app.storage.model.artifact import ArtifactModel
from app.storage.model.durable import DurableRunModel, ResumeCommandModel
from app.storage.model.human_input import HumanInputRequestModel, HumanInputResponseModel
from app.storage.model.log import LogEntryModel
from app.storage.model.task import CheckpointModel, EventModel, StepModel, TaskModel, TurnModel, WorkspaceModel
from app.storage.model.tool_execution import ToolCallModel, ToolExecutionModel
from app.storage.model.trace import TraceEventModel, TraceSpanModel


APP_MODELS = (
    WorkspaceModel,
    TaskModel,
    TurnModel,
    StepModel,
    EventModel,
    CheckpointModel,
    DurableRunModel,
    ResumeCommandModel,
    ApprovalRequestModel,
    ApprovalDecisionModel,
    HumanInputRequestModel,
    HumanInputResponseModel,
    ToolCallModel,
    ToolExecutionModel,
    ArtifactModel,
    TraceEventModel,
    TraceSpanModel,
)
LOG_MODELS = (LogEntryModel,)
LOG_SCHEMA_VERSION = 2


def initialize_app_schema(database_path: Path) -> Engine:
    """初始化主应用 SQLite schema。

    参数:
        database_path: 主应用数据库文件。

    返回:
        已初始化 schema 的 SQLAlchemy Engine。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果建表或迁移失败。

    副作用:
        创建主库目录、数据库文件和项目自有业务表；
        对已存在但缺少模型列的表执行就地 ALTER 补齐（schema 漂移自愈）。
    """

    engine = create_sqlite_engine(database_path)
    with engine.begin() as connection:
        for model in APP_MODELS:
            model.__table__.create(bind=connection, checkfirst=True)
        _ensure_model_columns(connection, engine)
    return engine


def _default_literal_for_type(column_type) -> str:
    """为缺失的 NOT NULL 列推导 SQLite 默认值字面量。

    参数:
        column_type: SQLAlchemy 列类型。

    返回:
        可直接拼接到 DDL 的默认值字面量（含引号或数字）。
    """

    type_name = str(column_type).upper()
    if "INT" in type_name or "BOOLEAN" in type_name:
        return "0"
    if "FLOAT" in type_name or "REAL" in type_name or "NUMERIC" in type_name:
        return "0.0"
    return "''"


def _ensure_model_columns(connection, engine) -> None:
    """就地补齐已存在表中缺失于模型的列，缓解 schema 漂移。

    仅对缺失列执行 ALTER TABLE ADD COLUMN；已存在的列与多余列均不动。
    可空列直接添加；NOT NULL 列在缺少 server_default 时按类型推导默认值，
    以保证存量行可通过约束。

    参数:
        connection: 当前事务连接。
        engine: SQLAlchemy Engine，用于获取方言以编译列类型。

    副作用:
        可能修改表结构（新增列）。
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
                column_ddl = f"{column.name} {ddl_type} NOT NULL DEFAULT {column.server_default.arg}"
            else:
                default = _default_literal_for_type(column.type)
                column_ddl = f"{column.name} {ddl_type} NOT NULL DEFAULT {default}"
            connection.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column_ddl}"))
            sys.stderr.write(f"[storage.schema] 为 {table.name} 补齐缺失列 {column.name}\n")


def initialize_log_schema(database_path: Path) -> Engine:
    """初始化日志 SQLite schema。

    参数:
        database_path: 日志数据库文件。

    返回:
        已初始化 schema 的 SQLAlchemy Engine。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果建表失败。

    副作用:
        创建日志库目录、数据库文件和日志表。
    """

    engine = create_sqlite_engine(database_path)
    with engine.begin() as connection:
        current_version = int(connection.execute(text("PRAGMA user_version")).scalar_one())
        if current_version == 0 and _has_table(connection, "log_entries"):
            _rebuild_log_schema(connection, current_version, LOG_SCHEMA_VERSION)
        elif current_version == 0:
            _create_log_schema(connection)
            connection.execute(text(f"PRAGMA user_version = {LOG_SCHEMA_VERSION}"))
        elif current_version < LOG_SCHEMA_VERSION:
            _rebuild_log_schema(connection, current_version, LOG_SCHEMA_VERSION)
    return engine


def _has_table(connection, table_name: str) -> bool:
    """返回目标连接中是否存在指定表。"""

    return inspect(connection).has_table(table_name)


def _create_log_schema(connection) -> None:
    """创建日志数据库 schema。"""

    for model in LOG_MODELS:
        model.__table__.create(bind=connection, checkfirst=True)
        for index in model.__table__.indexes:
            index.create(bind=connection, checkfirst=True)


def _rebuild_log_schema(connection, current_version: int, target_version: int) -> None:
    """重建日志数据库 schema。"""

    for model in LOG_MODELS:
        model.__table__.drop(bind=connection, checkfirst=True)
    _create_log_schema(connection)
    connection.execute(text(f"PRAGMA user_version = {target_version}"))
    sys.stderr.write(
        "[storage.schema] log schema rebuilt; "
        f"old_version={current_version}, new_version={target_version}\n"
    )
