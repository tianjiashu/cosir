"""SQLite schema 初始化。

绿地数据库只从当前 SQLAlchemy metadata 创建结构，不读取旧 schema，不执行兼容迁移。
"""

from typing import cast

from sqlalchemy import Engine, Table

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
from app.storage.model.workspace_model import WorkspaceModel
from app.storage.model.workspace_readiness_model import WorkspaceReadinessModel

APP_MODELS = (
    ProviderModel,
    ModelEntryModel,
    WorkspaceModel,
    WorkspaceReadinessModel,
    TaskModel,
    ConversationRunModel,
    ConversationCommandModel,
    ConversationTaskSnapshotModel,
    ConversationTaskContextModel,
    FileSnapshotModel,
    DelegationModel,
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
        在当前数据库创建缺失的应用表；不会读取、改写或保留旧对话表。
    """

    tables = [cast(Table, model.__table__) for model in APP_MODELS]
    StorageBase.metadata.create_all(engine, tables=tables)


def initialize_log_schema(engine: Engine) -> None:
    """创建日志库当前 metadata 声明的表。"""

    for model in LOG_MODELS:
        table = cast(Table, model.__table__)
        table.create(bind=engine, checkfirst=True)
        for index in table.indexes:
            index.create(bind=engine, checkfirst=True)
