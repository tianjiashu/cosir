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

from sqlalchemy import Engine, Table, inspect, text
from sqlalchemy.sql.schema import DefaultClause

from app.config.logging.logger import log
from app.storage.model.delegation_model import DelegationModel
from app.storage.model.file_snapshot_model import FileSnapshotModel
from app.storage.model.log_model import LogEntryModel
from app.storage.model.runtime_event_model import RuntimeEventModel
from app.storage.model.task_model import TaskModel
from app.storage.model.turn_message_model import TurnMessageModel
from app.storage.model.turn_model import TurnModel
from app.storage.model.workspace_model import WorkspaceModel

APP_MODELS = (
    WorkspaceModel,
    TaskModel,
    TurnModel,
    TurnMessageModel,
    RuntimeEventModel,
    FileSnapshotModel,
    DelegationModel,
)
LOG_MODELS = (LogEntryModel,)
LOG_SCHEMA_VERSION = 2


def initialize_app_schema(engine: Engine) -> None:
    """初始化主库（业务数据库）schema，并对已存在的表补齐缺失列与索引。

    完整流程（单事务，失败整体回滚）：
    1) 对 ``APP_MODELS`` 中的每个 model 执行“存在则跳过、不存在则建表”；
    2) 逐表比对模型定义与实际列，缺失的列以 ``ALTER TABLE ADD COLUMN`` 补齐
       （保守迁移，不删列、不改列）；
    3) 对存量 ``file_snapshots`` 执行一次性 seq 迁移（``_backfill_file_snapshot_task_seq``：
       删除孤儿快照、回填 task_id、按 ``(task_id, created_at, turn_id, seq, id)`` 重排
       seq），必须早于建索引，否则 ``(task_id, seq)`` 唯一索引在重叠存量数据上建不起来；
    4) 补建模型声明的新索引（``_ensure_model_indexes``）；
    5) 清理孤儿列与孤儿表（``_drop_orphan_task_columns`` / ``_drop_orphan_durable_runs``）。

    参数:
        engine: 已初始化的主库 SQLAlchemy 引擎（来自 ``engine_cache.create_sqlite_engine``）。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果建表、列迁移或 seq 迁移执行失败。

    副作用:
        创建缺失的业务表；对已存在的表追加缺失列、补建索引；清理孤儿列/孤儿表。
        除一次性 seq 迁移会重排存量快照的 task_id/seq、清理孤儿快照行外，业务数据
        不丢失。
    """

    with engine.begin() as connection:
        for model in APP_MODELS:
            cast(Table, model.__table__).create(bind=connection, checkfirst=True)
        _ensure_model_columns(connection, engine)
        _backfill_file_snapshot_task_seq(connection)
        _ensure_model_indexes(connection)
        _drop_orphan_task_columns(connection)
        _drop_orphan_durable_runs(connection)


def _drop_orphan_durable_runs(connection) -> None:
    """清理已被移除的 ``durable_runs`` 孤儿表。

    ``DurableRunModel`` 已从 ``APP_MODELS`` 中移除，因此不会被重建；但存量库升级时该表可能
    仍物理存在（保守迁移策略不删列、不删表）。此处一次性 ``DROP TABLE IF EXISTS`` 清理它，
    使存量库升级后无无人引用的孤儿表。该操作幂等、无业务数据损失（运行产物）。

    参数:
        connection: 当前处于事务中的 SQLAlchemy 连接。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果 DROP 执行失败。

    副作用:
        当 ``durable_runs`` 表存在时删除它。
    """

    if inspect(connection).has_table("durable_runs"):
        connection.execute(text("DROP TABLE IF EXISTS durable_runs"))
        log.info("dropped orphan table durable_runs")


def _backfill_file_snapshot_task_seq(connection) -> None:
    """一次性把存量 file_snapshots 迁移到「task 为 seq 命名空间」的新语义。

    旧版（seq 按 turn 内递增且表无 task_id 列）下，同 task 的跨 turn 快照 seq 相互
    重叠，跨 turn 聚合按 seq 排序时「最新变更」判定错乱，撤销可能还原到旧状态。本
    迁移做三件事（仅当 file_snapshots 与 turns 表均存在时执行，幂等）：

    1) 删除孤儿快照：所属 turn 已被删除的行无法归因任何 task，直接清理；
    2) 回填 task_id：每行取所属 turn 的 task_id；
    3) 重排 seq：按 (task_id, turn 创建顺序, turn_id, 原 seq, 行 id) 稳定排序后，
       把每个 task 内的快照重编号为连续递增 0..N-1，使 (task_id, seq) 在存量数据
       上唯一。排序键与 ``TurnCrud.list_by_task`` 对齐，保证迁移排序与运行时回放
       排序同构。

    必须早于 ``_ensure_model_indexes`` 执行：``uq_file_snapshots_task_seq`` 唯一索引
    只在存量数据满足唯一性后才能创建，否则建索引抛错导致整个迁移回滚。

    参数:
        connection: 当前处于事务中的 SQLAlchemy 连接。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果任一 SQL 执行失败。

    副作用:
        可能删除孤儿快照行、改写存量快照的 task_id 与 seq；写 info 日志。
    """
    # 防御：当前初始化流程保证 turns 先于 file_snapshots 建表，但函数被单独调用时
    # 仍要求两张表都存在，否则 JOIN/DELETE 会报 no such table。
    if not inspect(connection).has_table("file_snapshots") or not inspect(
        connection
    ).has_table("turns"):
        return
    deleted = connection.execute(
        text("DELETE FROM file_snapshots WHERE turn_id NOT IN (SELECT turn_id FROM turns)")
    )
    if deleted.rowcount:
        log.info("deleted %d orphan file_snapshots rows", deleted.rowcount)

    # ORDER BY 与 TurnCrud.list_by_task 的排序键 (created_at, turn_id) 对齐，保持
    # 「迁移排序」与「运行时回放排序」同构；seq/id 为同 task 内并列时的稳定兜底。
    rows = connection.execute(
        text(
            "SELECT fs.id AS fs_id, t.task_id AS task_id "
            "FROM file_snapshots fs JOIN turns t ON t.turn_id = fs.turn_id "
            "ORDER BY t.task_id, t.created_at, t.turn_id, fs.seq, fs.id"
        )
    ).fetchall()
    if not rows:
        return
    seq_by_task: dict[str, int] = {}
    for row in rows:
        fs_id = row[0]
        task_id = row[1]
        nxt = seq_by_task.get(task_id, 0)
        connection.execute(
            text("UPDATE file_snapshots SET task_id = :tid, seq = :seq WHERE id = :id"),
            {"tid": task_id, "seq": nxt, "id": fs_id},
        )
        seq_by_task[task_id] = nxt + 1
    log.info("backfilled file_snapshots task_id/seq for %d rows", len(rows))


def _drop_orphan_task_columns(connection) -> None:
    """清理 tasks 表中已移除的孤儿列，使存量库升级后无无人引用的孤儿字段。

    这些列（``input_text`` / ``last_message_preview`` / ``latest_turn_id``）语义上属于轮次
    维度或已被判定不应由任务持有，已从 ``TaskModel`` 与 ``TaskResponse`` 中移除。存量库升级时
    这些列可能仍物理存在（保守迁移策略不删列），此处一次性 DROP 收口，与模型现状对齐。

    参数:
        connection: 当前处于事务中的 SQLAlchemy 连接。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果 DROP COLUMN 执行失败。

    副作用:
        当 tasks 表存在且含上述孤儿列时逐个删除；每删除一列写一条 info 日志。
    """
    _ORPHAN_TASK_COLUMNS = ("input_text", "last_message_preview", "latest_turn_id")
    if not inspect(connection).has_table("tasks"):
        return
    existing = {col["name"] for col in inspect(connection).get_columns("tasks")}
    for column_name in _ORPHAN_TASK_COLUMNS:
        if column_name not in existing:
            continue
        connection.execute(
            text(f"ALTER TABLE tasks DROP COLUMN {column_name}")
        )
        log.info("dropped orphan column tasks.%s", column_name)


def _default_literal_for_type(column_type) -> str:
    """为“新增的 NOT NULL 列”推导一个 SQLite 默认值字面量。

    SQLite 对已存在数据的表新增 NOT NULL 列时必须提供 DEFAULT，否则历史行无法满足非空约束。
    本函数按列类型给出安全的零值：整型 / 布尔为 ``0``，浮点 / 数值为 ``0.0``，其余（文本等）
    为空字符串 ``''``。

    参数:
        column_type: SQLAlchemy 列类型对象。

    返回:
        可直接拼进 ``ALTER TABLE ... DEFAULT`` 的 SQL 字面量字符串。

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


def _ensure_model_columns(connection, engine) -> None:
    """把 model 中新增、但数据库表里尚缺的列补齐到已存在的主库表。

    逐个遍历 ``APP_MODELS``：表不存在则跳过（建表逻辑由 ``initialize_app_schema`` 负责）；
    表存在则比对实际列与模型列，对每个缺失列拼装 DDL 并执行 ``ALTER TABLE ADD COLUMN``。
    列是否可空 / 是否有 server_default 决定 DDL 形态：可空列直接加；带 server_default 的
    NOT NULL 列使用其默认值；无默认值的 NOT NULL 列回退到 ``_default_literal_for_type``
    推导的零值默认。

    参数:
        connection: 当前处于事务中的 SQLAlchemy 连接。
        engine: 用于按方言编译列类型 DDL 的 SQLAlchemy 引擎。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果 ALTER TABLE 执行失败。

    副作用:
        可能对已存在的表追加列；每追加一列写一条 info 日志。
    """

    inspector = inspect(connection)
    for model in APP_MODELS:
        table = cast(Table, model.__table__)
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
                default_arg = cast(DefaultClause, column.server_default).arg
                column_ddl = f"{column.name} {ddl_type} NOT NULL DEFAULT {default_arg}"
            else:
                default = _default_literal_for_type(column.type)
                column_ddl = f"{column.name} {ddl_type} NOT NULL DEFAULT {default}"
            connection.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column_ddl}"))
            log.info("added missing column %s.%s", table.name, column.name)


def _ensure_model_indexes(connection) -> None:
    """补齐 model 声明、但存量库里尚未创建的索引。

    迁移补列步骤 ``_ensure_model_columns`` 只负责 ``ALTER TABLE ADD COLUMN``，不会创建
    ``__table_args__`` 里声明的索引 / 唯一约束（如 ``idx_tasks_parent_task_id``、
    ``uq_tasks_delegation_id``）。存量库升级时这些索引因此缺失，导致并发重入的唯一索引
    兜底形同虚设。本函数遍历 ``APP_MODELS`` 每个 table 的声明索引，对库里不存在的同名索引
    调用 ``index.create(checkfirst=True)`` 补齐。

    必须在 ``_ensure_model_columns`` 之后调用：索引依赖其引用的新列已存在，否则创建会失败。

    参数:
        connection: 当前处于事务中的 SQLAlchemy 连接。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果读取库结构元数据或创建索引执行失败。

    副作用:
        可能对已存在的表补建缺失索引；每成功补建一个索引写一条 info 日志。
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
    """初始化日志库 schema，必要时按版本号重建。

    使用 SQLite 内置的 ``PRAGMA user_version`` 作为日志库结构版本号，按三种情况处理：
    1. 版本为 0 且已存在旧的 ``log_entries`` 表：视为“无版本号的历史结构”，直接重建到当前版本；
    2. 版本为 0 且无旧表：首次初始化，建表并写入当前版本号；
    3. 版本号小于当前目标版本：结构落后，重建到当前版本。
    日志库允许整表重建是因为历史日志属可丢弃的运行产物（见模块 docstring）。整个过程在单个
    事务中完成。

    参数:
        engine: 已初始化的日志库 SQLAlchemy 引擎（来自 ``engine_cache.create_sqlite_engine``）。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果建表或重建执行失败。

    副作用:
        创建或重建日志表及其索引，并更新 ``PRAGMA user_version``；重建会丢弃旧日志数据。
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
    """判断当前连接对应的数据库中是否存在指定表。

    参数:
        connection: 活动的 SQLAlchemy 连接。
        table_name: 待检查的表名。

    返回:
        表存在返回 True，否则返回 False。

    异常:
        无。

    副作用:
        无（仅读取库结构元数据）。
    """

    return inspect(connection).has_table(table_name)


def _create_log_schema(connection) -> None:
    """创建日志库的表与索引。

    对 ``LOG_MODELS`` 中的每个 model 建表（存在则跳过），并逐个创建其声明的索引。

    参数:
        connection: 处于事务中的 SQLAlchemy 连接。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果建表或建索引失败。

    副作用:
        在日志库中创建缺失的日志表与索引。
    """

    for model in LOG_MODELS:
        table = cast(Table, model.__table__)
        table.create(bind=connection, checkfirst=True)
        for index in table.indexes:
            index.create(bind=connection, checkfirst=True)


def _rebuild_log_schema(connection, current_version: int, target_version: int) -> None:
    """重建日志库 schema：先删旧表再建新表，并写入目标版本号。

    用于日志库结构落后（或无版本号）时的整表演进。历史日志会被丢弃，这是日志库的既定策略
    （见模块 docstring）。

    参数:
        connection: 处于事务中的 SQLAlchemy 连接。
        current_version: 重建前的 ``user_version``，仅用于日志记录。
        target_version: 重建后写入的目标 ``user_version``。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果删表 / 建表 / 版本写入失败。

    副作用:
        删除并重建日志表与索引，更新 ``PRAGMA user_version``，并写一条 info 日志；旧日志数据丢失。
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
