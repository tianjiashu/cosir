"""SQLite schema 初始化与轻量迁移。

单一职责：负责“建表”与“把已存在的旧表演进到当前模型定义”，即主库 schema 的创建 + 列级
补齐，以及日志库 schema 的创建 + 版本化重建。所有表结构以 ``app.storage.model`` 下的
SQLAlchemy model 为单一事实来源，本模块只做“让数据库结构追上 model 定义”的动作。

为什么主库和日志库迁移策略不同：
    - 主库（业务数据）不能丢数据，因此采用“加列不删列”的保守策略：只对缺失列执行
      ``ALTER TABLE ADD COLUMN``，历史数据原样保留。
    - 日志库是可重建的运行产物，历史日志不具备长期价值，因此采用 ``user_version``
      版本号驱动的“整表重建”策略：版本落后时直接 drop 后重建，简单且无历史包袱。

职责边界：
    - 负责：建表、缺失列补齐、日志库版本重建。
    - 不负责：引擎创建与连接池（见 ``engine_cache``）、引擎编排与生命周期
      （见 ``store_engines``）、业务读写（见 ``crud/``）。

调用时机：由 ``store_engines.init_storage`` 在进程启动时对主库、日志库各调用一次。
"""

from typing import cast

from sqlalchemy import Connection, Engine, Table, inspect, text
from sqlalchemy.sql.schema import DefaultClause

from app.config.logging.logger import log
from app.storage.model.delegation_model import DelegationModel
from app.storage.model.file_snapshot_model import FileSnapshotModel
from app.storage.model.log_model import LogEntryModel
from app.storage.model.model_entry_model import ModelEntryModel
from app.storage.model.provider_model import ProviderModel
from app.storage.model.runtime_event_model import RuntimeEventModel
from app.storage.model.task_model import TaskModel
from app.storage.model.turn_message_model import TurnMessageModel
from app.storage.model.turn_model import TurnModel
from app.storage.model.workspace_model import WorkspaceModel

APP_MODELS = (
    # providers / models 是模型配置的事实来源，先于业务表创建；models 表外键依赖 providers。
    ProviderModel,
    ModelEntryModel,
    WorkspaceModel,
    TaskModel,
    TurnModel,
    TurnMessageModel,
    RuntimeEventModel,
    FileSnapshotModel,
    DelegationModel,
)

# 当前没有「仅迁移不建表」的模型：所有业务表统一由 APP_MODELS 负责创建与演进。
_MIGRATE_ONLY_MODELS = ()
LOG_MODELS = (LogEntryModel,)
LOG_SCHEMA_VERSION = 2


def initialize_app_schema(engine: Engine) -> None:
    """初始化主库 schema，并对已存在的表补齐缺失列与索引。

    流程（单事务，失败整体回滚）：①对 ``APP_MODELS`` 逐个建表（存在则跳过）；
    ②``_ensure_model_columns`` 逐表比对模型与实际列，``ADD COLUMN`` 补齐缺失列
    （保守迁移不删数据列，仅按 ``_COLUMN_RENAME_MAP`` 做历史重命名）；
    ③``_ensure_model_indexes`` 补建模型声明的缺失索引。当前 ``_MIGRATE_ONLY_MODELS``
    为空，保留该参数仅作为未来「仅迁移不建表」模型的扩展占位。

    参数:
        engine: 已初始化的主库 SQLAlchemy 引擎（来自 ``engine_cache.create_sqlite_engine``）。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果建表、列迁移或建索引执行失败。

    副作用:
        创建缺失业务表；对已存在表追加缺失列、补建索引。业务数据不丢失。
    """

    with engine.begin() as connection:
        for model in APP_MODELS:
            cast(Table, model.__table__).create(bind=connection, checkfirst=True)
        # 核心表建表后，对 APP_MODELS 全量做列迁移与重命名（含 providers 表的
        # api_key_env → api_key 历史漂移）。_MIGRATE_ONLY_MODELS 目前为空。
        _ensure_model_columns(connection, engine, APP_MODELS + _MIGRATE_ONLY_MODELS)
        _ensure_model_indexes(connection)


def _default_literal_for_type(column_type) -> str:
    """为新增的 NOT NULL 列推导 SQLite 默认值字面量。

    SQLite 对已有数据的表新增 NOT NULL 列必须给 DEFAULT。按列类型返回零值：
    整型/布尔 ``0``，浮点/数值 ``0.0``，其余 ``''``。

    参数:
        column_type: SQLAlchemy 列类型对象。

    返回:
        可拼进 ``ALTER TABLE ... DEFAULT`` 的 SQL 字面量字符串。

    异常:
        无。

    副作用:
        无。
    """

    type_name = str(column_type).upper()
    if "INT" in type_name or "BOOLEAN" in type_name:
        return "0"
    if "FLOAT" in type_name or "REAL" in type_name or "NUMERIC" in type_name:
        return "0.0"
    return "''"


# 已知历史列名重命名映射（按表名 → {旧列名: 新列名}）。
# 仅收录经过确认的「重命名类」迁移：普通新增列走 _ensure_model_columns 的 ADD COLUMN，
# 无需登记；但旧列被改名（而非新增）的场景无法靠补列覆盖，必须显式 RENAME。
# 当前唯一登记项：providers 表的 api_key_env → api_key（2026-08-18 确认的历史漂移）。
_COLUMN_RENAME_MAP: dict[str, dict[str, str]] = {
    "providers": {"api_key_env": "api_key"},
}


def _ensure_model_columns(
    connection: Connection, engine: Engine, models=None
) -> None:
    """补齐已存在主库表中模型新增但库内尚缺的列。

    逐个遍历 ``models``（缺省 ``APP_MODELS``）：表不存在则跳过；存在则比对实际列与模型列，
    对每个缺失列执行 ``ALTER TABLE ADD COLUMN``。可空列直接加；带 server_default 的
    NOT NULL 列用其默认值；否则回退 ``_default_literal_for_type`` 的零值默认。

    参数:
        connection: 当前处于事务中的 SQLAlchemy 连接。
        engine: 用于按方言编译列类型 DDL 的引擎。
        models: 参与列迁移的 ORM 模型序列，缺省 ``APP_MODELS``；可传
            ``APP_MODELS + _MIGRATE_ONLY_MODELS`` 覆盖额外需要列迁移的表。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果 ALTER TABLE 执行失败。

    副作用:
        对已存在表追加缺失列；每列一条 info 日志。
    """

    if models is None:
        models = APP_MODELS
    inspector = inspect(connection)
    for model in models:
        table = cast(Table, model.__table__)
        if not inspector.has_table(table.name):
            continue
        existing = {col["name"] for col in inspector.get_columns(table.name)}
        # 已知历史列名重命名的预处理：库里是旧列名、ORM 期望新列名时，先把旧列改名，
        # 使其后续被「列已存在」分支跳过，避免重复 ADD COLUMN。仅收录经过确认的、
        # 无法用「加列」覆盖的重命名类迁移，集中登记便于审计。
        old_to_new = _COLUMN_RENAME_MAP.get(table.name, {})
        for old_name, new_name in old_to_new.items():
            if old_name in existing and new_name not in existing:
                connection.execute(
                    text(f"ALTER TABLE {table.name} RENAME COLUMN {old_name} TO {new_name}")
                )
                log.info("renamed column %s.%s -> %s", table.name, old_name, new_name)
                existing.discard(old_name)
                existing.add(new_name)
        for column in table.columns:
            if column.name in existing:
                continue
            ddl_type = column.type.compile(dialect=engine.dialect)
            if column.nullable:
                column_ddl = f"{column.name} {ddl_type}"
            elif column.server_default is not None:
                default_arg = cast(DefaultClause, column.server_default).arg
                column_ddl = f"{column.name} {ddl_type} NOT NULL DEFAULT {default_arg}"
            else:
                default = _default_literal_for_type(column.type)
                column_ddl = f"{column.name} {ddl_type} NOT NULL DEFAULT {default}"
            connection.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column_ddl}"))
            log.info("added missing column %s.%s", table.name, column.name)


def _ensure_model_indexes(connection) -> None:
    """补齐模型声明但存量库里尚未创建的索引。

    遍历 ``APP_MODELS`` 每个 table 的声明索引，对库中不存在的同名索引
    ``index.create(checkfirst=True)`` 补齐（如 ``idx_tasks_parent_task_id``、
    ``uq_tasks_delegation_id``）。必须在 ``_ensure_model_columns`` 之后调用：索引依赖其
    引用的新列已存在，否则创建失败。

    参数:
        connection: 当前处于事务中的 SQLAlchemy 连接。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果读取元数据或创建索引失败。

    副作用:
        对已存在表补建缺失索引；每建一个写一条 info 日志。
    """

    inspector = inspect(connection)
    for model in APP_MODELS:
        table = cast(Table, model.__table__)
        if not inspector.has_table(table.name):
            continue
        existing_indexes = {
            idx["name"] for idx in inspector.get_indexes(table.name)
        }
        for index in table.indexes:
            if index.name in existing_indexes:
                continue
            index.create(bind=connection, checkfirst=True)
            log.info("created missing index %s on %s", index.name, table.name)


def initialize_log_schema(engine: Engine) -> None:
    """初始化日志库 schema，按 ``PRAGMA user_version`` 版本号决定建表或重建。

    版本 0：有旧 ``log_entries`` 表则视为无版本历史结构、直接重建；无旧表则首次建表并写版本号。
    版本小于目标版本：重建到当前版本。日志库可重建因历史日志是可丢弃的运行产物（见模块 docstring）。

    参数:
        engine: 已初始化的日志库 SQLAlchemy 引擎。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果建表或重建失败。

    副作用:
        创建或重建日志表及索引并更新版本号；重建丢弃旧日志数据。
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
    """判断当前连接数据库中是否存在指定表。

    参数:
        connection: 活动的 SQLAlchemy 连接。
        table_name: 待检查的表名。

    返回:
        存在返回 True，否则 False。

    异常:
        无。

    副作用:
        无（仅读库结构元数据）。
    """

    return inspect(connection).has_table(table_name)


def _create_log_schema(connection) -> None:
    """创建日志库的表与索引。

    对 ``LOG_MODELS`` 每个 model 建表（存在则跳过）并创建其声明索引。

    参数:
        connection: 处于事务中的 SQLAlchemy 连接。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果建表或建索引失败。

    副作用:
        创建缺失的日志表与索引。
    """

    for model in LOG_MODELS:
        table = cast(Table, model.__table__)
        table.create(bind=connection, checkfirst=True)
        for index in table.indexes:
            index.create(bind=connection, checkfirst=True)


def _rebuild_log_schema(connection, current_version: int, target_version: int) -> None:
    """重建日志库 schema：删旧表、建新表，并写入目标版本号。

    用于结构落后（或无版本号）时的整表演进；历史日志丢弃（既定策略，见模块 docstring）。

    参数:
        connection: 处于事务中的 SQLAlchemy 连接。
        current_version: 重建前 ``user_version``，仅用于日志记录。
        target_version: 重建后写入的目标 ``user_version``。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果删表 / 建表 / 写版本失败。

    副作用:
        重建日志表及索引、更新版本号并写 info 日志；旧日志数据丢失。
    """

    for model in LOG_MODELS:
        cast(Table, model.__table__).drop(bind=connection, checkfirst=True)
    _create_log_schema(connection)
    connection.execute(text(f"PRAGMA user_version = {target_version}"))
    log.info(
        "log schema rebuilt; old_version=%s, new_version=%s",
        current_version,
        target_version,
    )
