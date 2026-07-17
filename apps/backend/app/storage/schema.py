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
from app.storage.model.task import CheckpointModel, EventModel, SessionModel, StepModel, TaskModel, TurnModel
from app.storage.model.tool_execution import ToolCallModel, ToolExecutionModel
from app.storage.model.trace import TraceEventModel, TraceSpanModel


APP_MODELS = (
    SessionModel,
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
        sqlalchemy.exc.SQLAlchemyError: 如果建表失败。

    副作用:
        创建主库目录、数据库文件和项目自有业务表。
    """

    engine = create_sqlite_engine(database_path)
    with engine.begin() as connection:
        for model in APP_MODELS:
            model.__table__.create(bind=connection, checkfirst=True)
    return engine


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
