"""``init_schema`` 列迁移逻辑进阶 / 对抗性测试。

聚焦 SQLite 主库 ``initialize_app_schema`` / ``_ensure_model_columns`` 对「用户机旧库」
的列级演进：

- 重命名迁移：providers.api_key_env -> api_key，存量数据保留；
- 补列迁移：缺失 ORM 列被 ADD COLUMN，现有列不删；
- 幂等性：已最新 schema 的库重复调用不报错；
- NOT NULL 列补列：enabled(NOT NULL DEFAULT 1) 补列不应丢数据；
- 数据零丢失：迁移前后行数与关键字段值不变。

每个用例上方一行注释说明测试目的与潜在缺陷类型。
"""

import sqlite3
from pathlib import Path

from sqlalchemy import Boolean, Engine, Float, Integer, Numeric, Text, create_engine, inspect, text

from app.storage.init_schema import (
    _default_literal_for_type,
    _ensure_model_columns,
    initialize_app_schema,
    initialize_log_schema,
)
from app.storage.model.provider_model import ProviderModel


# --------------------------------------------------------------------------- #
# 公共工具
# --------------------------------------------------------------------------- #
def _make_engine(tmp_path: Path) -> Engine:
    """创建一个临时 SQLite 引擎用于迁移测试（不强制 WAL/FK PRAGMA，迁移本身不依赖）。"""
    db_path = tmp_path / "app.sqlite3"
    return create_engine(f"sqlite:///{db_path}")


def _raw_cols(engine: Engine, table: str) -> set[str]:
    """读取已提交后的表列名集合。"""
    with engine.connect() as conn:
        return {col["name"] for col in inspect(conn).get_columns(table)}


def _raw_rows(engine: Engine, sql: str):
    """读取已提交后的查询结果。"""
    with engine.connect() as conn:
        return conn.execute(text(sql)).fetchall()


def _drop_providers(connection) -> None:
    connection.execute(text("DROP TABLE IF EXISTS providers"))


def _create_full_legacy_providers(connection) -> None:
    """构造一张「相对完整旧库」providers：含 api_key_env、api_version、含 created_at/updated_at，缺 api_key。"""
    _drop_providers(connection)
    connection.execute(
        text(
            "CREATE TABLE providers ("
            "provider_id TEXT PRIMARY KEY, "
            "name TEXT NOT NULL, "
            "type TEXT NOT NULL, "
            "base_url TEXT, "
            "api_key_env TEXT, "
            "enabled INTEGER NOT NULL DEFAULT 1, "
            "sort_order INTEGER NOT NULL DEFAULT 0, "
            "api_version TEXT, "
            "created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL"
            ")"
        )
    )
    connection.execute(
        text(
            "INSERT INTO providers "
            "(provider_id, name, type, base_url, api_key_env, enabled, sort_order, api_version, created_at, updated_at) "
            "VALUES "
            "('p1', 'DeepSeek', 'deepseek', 'https://api.deepseek.com', 'sk-legacy-1', 1, 0, 'v1', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'), "
            "('p2', 'OpenAI', 'openai-compatible', 'https://api.openai.com', 'sk-legacy-2', 0, 5, 'v1', '2026-01-02T00:00:00Z', '2026-01-02T00:00:00Z')"
        )
    )


def _create_sparse_providers(connection) -> None:
    """构造一张只含 provider_id / name 的最小旧表，缺 type / api_key 等 ORM 列。"""
    _drop_providers(connection)
    connection.execute(
        text(
            "CREATE TABLE providers ("
            "provider_id TEXT PRIMARY KEY, "
            "name TEXT NOT NULL"
            ")"
        )
    )
    connection.execute(
        text(
            "INSERT INTO providers (provider_id, name) VALUES "
            "('p1', 'DeepSeek'), ('p2', 'OpenAI')"
        )
    )


# --------------------------------------------------------------------------- #
# 1. 重命名迁移：api_key_env -> api_key，存量数据正确迁移
# --------------------------------------------------------------------------- #
def test_rename_api_key_env_to_api_key_with_data(tmp_path: Path) -> None:
    """目的：验证 api_key_env 旧列被重命名为 api_key 且存量 api_key_env 值迁移到新列；潜在缺陷：重命名未迁移存量值/列名残留。"""
    engine = _make_engine(tmp_path)
    with engine.begin() as connection:
        _create_full_legacy_providers(connection)

    initialize_app_schema(engine)

    cols = _raw_cols(engine, "providers")
    assert "api_key" in cols, "api_key_env 应被重命名为 api_key"
    assert "api_key_env" not in cols, "旧列名 api_key_env 应已消失"
    # 存量数据随重命名迁移到新列
    rows = _raw_rows(
        engine,
        "SELECT provider_id, api_key FROM providers ORDER BY provider_id",
    )
    assert dict(rows) == {"p1": "sk-legacy-1", "p2": "sk-legacy-2"}, (
        f"api_key 存量值应=旧 api_key_env 值，实际 {rows}"
    )


def test_rename_preserves_other_column_data(tmp_path: Path) -> None:
    """目的：重命名迁移后其余列（name/type/base_url/enabled）的存量值保持不变；潜在缺陷：RENAME 触发的 DDL 误删/覆盖其他列。"""
    engine = _make_engine(tmp_path)
    with engine.begin() as connection:
        _create_full_legacy_providers(connection)

    initialize_app_schema(engine)

    rows = _raw_rows(
        engine,
        "SELECT provider_id, name, type, base_url, enabled FROM providers ORDER BY provider_id",
    )
    result = {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}
    assert result == {
        "p1": ("DeepSeek", "deepseek", "https://api.deepseek.com", 1),
        "p2": ("OpenAI", "openai-compatible", "https://api.openai.com", 0),
    }, f"重命名后其余列存量值应保持不变，实际 {result}"


def test_rename_idempotent_when_already_api_key(tmp_path: Path) -> None:
    """目的：旧库已存在 api_key（无 api_key_env）时迁移不应误删/误改 api_key；潜在缺陷：新列已存在时仍执行 RENAME 或重复 ADD。"""
    engine = _make_engine(tmp_path)
    with engine.begin() as connection:
        _drop_providers(connection)
        connection.execute(
            text(
                "CREATE TABLE providers ("
                "provider_id TEXT PRIMARY KEY, "
                "name TEXT NOT NULL, "
                "type TEXT NOT NULL, "
                "api_key TEXT, "
                "enabled INTEGER NOT NULL DEFAULT 1, "
                "created_at TEXT NOT NULL, "
                "updated_at TEXT NOT NULL"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT INTO providers (provider_id, name, type, api_key, created_at, updated_at) "
                "VALUES ('p1', 'DeepSeek', 'deepseek', 'sk-existing', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
        )

    initialize_app_schema(engine)

    cols = _raw_cols(engine, "providers")
    assert "api_key" in cols and "api_key_env" not in cols
    rows = _raw_rows(engine, "SELECT provider_id, api_key FROM providers")
    assert dict(rows) == {"p1": "sk-existing"}, f"已有 api_key 值应被原样保留，实际 {rows}"


# --------------------------------------------------------------------------- #
# 2. 补列迁移：缺失 ORM 列被 ADD，现有列不删
# --------------------------------------------------------------------------- #
def test_ensure_model_columns_adds_missing_no_drop(tmp_path: Path) -> None:
    """目的：_ensure_model_columns 补齐缺失 ORM 列（type/api_key 等）且不删除现有列；潜在缺陷：补列误删现有列或遗漏缺失列。"""
    engine = _make_engine(tmp_path)
    with engine.begin() as connection:
        _create_sparse_providers(connection)

    with engine.begin() as connection:
        _ensure_model_columns(connection, engine, (ProviderModel,))

    cols = _raw_cols(engine, "providers")
    # 现有列保留
    assert "provider_id" in cols and "name" in cols, "现有列不应被删除"
    # 缺失的 ORM 列应补齐
    for expected in ("type", "api_key", "enabled", "sort_order", "created_at", "updated_at"):
        assert expected in cols, f"缺失的 ORM 列 {expected} 应被 ADD COLUMN"


def test_ensure_model_columns_preserves_existing_rows(tmp_path: Path) -> None:
    """目的：补列不删数据，存量行在补列后保留；潜在缺陷：ADD COLUMN 在非空表上触发约束导致丢行/失败。"""
    engine = _make_engine(tmp_path)
    with engine.begin() as connection:
        _create_sparse_providers(connection)

    with engine.begin() as connection:
        _ensure_model_columns(connection, engine, (ProviderModel,))

    rows = _raw_rows(engine, "SELECT provider_id, name FROM providers ORDER BY provider_id")
    assert rows == [("p1", "DeepSeek"), ("p2", "OpenAI")], f"补列后存量行应保留，实际 {rows}"


def test_minimal_table_no_extra_columns_added_wrongly(tmp_path: Path) -> None:
    """目的：补列只补齐 ORM 声明的列，不臆造无关列；潜在缺陷：补列逻辑误加非模型列。"""
    engine = _make_engine(tmp_path)
    with engine.begin() as connection:
        _create_sparse_providers(connection)

    with engine.begin() as connection:
        _ensure_model_columns(connection, engine, (ProviderModel,))

    cols = _raw_cols(engine, "providers")
    # ORM 声明的 providers 列集合
    model_cols = {c.name for c in ProviderModel.__table__.columns}
    assert cols == model_cols, f"补列后表列集合应=ORM 列集合，实际 表={cols} 模型={model_cols}"


# --------------------------------------------------------------------------- #
# 4. 幂等性：已是最新 schema 的库重复调用不报错
# --------------------------------------------------------------------------- #
def test_initialize_app_schema_idempotent_fresh_db(tmp_path: Path) -> None:
    """目的：全新建表场景（含 providers 由 CRUD 建表）下重复调用 initialize_app_schema 不报错；潜在缺陷：幂等缺失导致重复补列/重命名抛错。

    注意：providers 属 _MIGRATE_ONLY_MODELS，initialize_app_schema 的建表循环只覆盖
    APP_MODELS，providers 的建表由各自 CRUD 负责（见 init_schema 模块注释）。此处用
    ProviderModel.__table__.create 模拟 CRUD 建表职责，使 providers 在调用前已存在。
    """
    engine = _make_engine(tmp_path)

    # 模拟运行时：CRUD 先为 providers 建表（完整 ORM 结构），其余 APP_MODELS 由 initialize_app_schema 建
    with engine.begin() as connection:
        ProviderModel.__table__.create(bind=connection, checkfirst=True)

    # 第一次：APP_MODELS 建表 + 全量列迁移（providers 已是完整结构，应无需补列）
    initialize_app_schema(engine)
    cols_after_first = _raw_cols(engine, "providers")
    assert "api_key" in cols_after_first

    # 第二次：已是目标 schema，重复调用应无异常
    initialize_app_schema(engine)

    cols_after_second = _raw_cols(engine, "providers")
    assert cols_after_second == cols_after_first, "重复调用后列集合应保持一致"


def test_initialize_app_schema_idempotent_after_legacy_migration(tmp_path: Path) -> None:
    """目的：旧库迁移到最新 schema 后再调用一次不应报错（端到端幂等）；潜在缺陷：重命名分支在已迁移库上重入报错。"""
    engine = _make_engine(tmp_path)
    with engine.begin() as connection:
        _create_full_legacy_providers(connection)

    initialize_app_schema(engine)
    # 再调用一次
    initialize_app_schema(engine)

    cols = _raw_cols(engine, "providers")
    assert "api_key" in cols and "api_key_env" not in cols
    rows = _raw_rows(
        engine, "SELECT provider_id, api_key FROM providers ORDER BY provider_id"
    )
    assert dict(rows) == {"p1": "sk-legacy-1", "p2": "sk-legacy-2"}, (
        f"幂等重入后存量数据应保持，实际 {rows}"
    )


# --------------------------------------------------------------------------- #
# 5. NOT NULL 列补列：enabled(NOT NULL DEFAULT 1) 补列不丢数据
# --------------------------------------------------------------------------- #
def test_add_not_null_default_column_keeps_rows(tmp_path: Path) -> None:
    """目的：为含数据的表补 NOT NULL DEFAULT 1 的 enabled 列，存量行保留且新列取值=默认；潜在缺陷：NOT NULL 无默认导致补列失败或存量行被标脏。"""
    engine = _make_engine(tmp_path)
    with engine.begin() as connection:
        # 缺 enabled 的 providers（含其它 NOT NULL 列以贴近真实），并预置数据
        _drop_providers(connection)
        connection.execute(
            text(
                "CREATE TABLE providers ("
                "provider_id TEXT PRIMARY KEY, "
                "name TEXT NOT NULL, "
                "type TEXT NOT NULL, "
                "api_key TEXT, "
                "created_at TEXT NOT NULL, "
                "updated_at TEXT NOT NULL"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT INTO providers (provider_id, name, type, api_key, created_at, updated_at) "
                "VALUES ('p1', 'DeepSeek', 'deepseek', 'sk-1', 't', 't'), "
                "('p2', 'OpenAI', 'openai-compatible', 'sk-2', 't', 't')"
            )
        )

    with engine.begin() as connection:
        _ensure_model_columns(connection, engine, (ProviderModel,))

    rows = _raw_rows(
        engine, "SELECT provider_id, name, enabled FROM providers ORDER BY provider_id"
    )
    result = {r[0]: (r[1], r[2]) for r in rows}
    # 存量行保留；enabled 作为 NOT NULL DEFAULT 1 补列后应为 1
    assert result == {
        "p1": ("DeepSeek", 1),
        "p2": ("OpenAI", 1),
    }, f"NOT NULL DEFAULT 1 补列后存量行保留且 enabled=1，实际 {result}"


def test_not_null_no_default_adds_zero_value_fallback(tmp_path: Path) -> None:
    """目的：验证 NOT NULL 且无 server_default 的列(type/created_at/updated_at)补列时使用零值兜底，存量行被填充空串而非报错；潜在缺陷：无默认 NOT NULL 补列在存量表上失败。"""
    engine = _make_engine(tmp_path)
    with engine.begin() as connection:
        _create_sparse_providers(connection)  # 只含 provider_id/name，缺 type/created_at/updated_at

    with engine.begin() as connection:
        _ensure_model_columns(connection, engine, (ProviderModel,))

    # 不应抛错；存量行保留
    rows = _raw_rows(
        engine, "SELECT provider_id, name FROM providers ORDER BY provider_id"
    )
    assert rows == [("p1", "DeepSeek"), ("p2", "OpenAI")], f"零值兜底补列后存量行应保留，实际 {rows}"

    # 暴露设计权衡：无默认 NOT NULL 列被填零值（空串），存量语义信息丢失
    rows_full = _raw_rows(
        engine, "SELECT provider_id, type, created_at, updated_at FROM providers ORDER BY provider_id"
    )
    for r in rows_full:
        # 这些非空列被填 '' —— 记录为潜在数据污染点（非断言失败，仅观测）
        assert r[1] == "" and r[2] == "" and r[3] == "", (
            f"无默认 NOT NULL 列补列后被零值兜底为空串：{r}"
        )


# --------------------------------------------------------------------------- #
# 6. 数据零丢失：迁移前后 providers 表行数与关键字段值不变
# --------------------------------------------------------------------------- #
def test_row_count_and_key_fields_preserved(tmp_path: Path) -> None:
    """目的：完整旧库经 initialize_app_schema 后行数不变、关键字段(name/api_key/enabled)值不变；潜在缺陷：迁移过程中意外删行或改写关键字段。"""
    engine = _make_engine(tmp_path)
    with engine.begin() as connection:
        _create_full_legacy_providers(connection)

    before = _raw_rows(
        engine,
        "SELECT provider_id, name, type, base_url, api_key_env, enabled, api_version "
        "FROM providers ORDER BY provider_id",
    )
    before_dict = {
        r[0]: {"name": r[1], "type": r[2], "base_url": r[3], "key_env": r[4], "enabled": r[5]}
        for r in before
    }
    before_count = len(before)

    initialize_app_schema(engine)

    after = _raw_rows(
        engine,
        "SELECT provider_id, name, type, base_url, api_key, enabled "
        "FROM providers ORDER BY provider_id",
    )
    after_count = len(after)
    after_dict = {r[0]: {"name": r[1], "type": r[2], "base_url": r[3], "api_key": r[4], "enabled": r[5]} for r in after}

    assert after_count == before_count, f"行数应保持不变，前={before_count} 后={after_count}"

    for pid in before_dict:
        b = before_dict[pid]
        a = after_dict[pid]
        assert a["name"] == b["name"], f"[{pid}] name 应不变：{a['name']} != {b['name']}"
        assert a["type"] == b["type"], f"[{pid}] type 应不变"
        assert a["base_url"] == b["base_url"], f"[{pid}] base_url 应不变"
        assert a["enabled"] == b["enabled"], f"[{pid}] enabled 应不变"
        # api_key 应承接原 api_key_env 值
        assert a["api_key"] == b["key_env"], (
            f"[{pid}] 迁移后 api_key 应=原 api_key_env({b['key_env']})，实际 {a['api_key']}"
        )


def test_empty_providers_table_migration_no_error(tmp_path: Path) -> None:
    """目的：providers 表为空（0 行）时迁移不报错；潜在缺陷：空表补列/重命名边界未处理。"""
    engine = _make_engine(tmp_path)
    with engine.begin() as connection:
        _create_full_legacy_providers(connection)
        connection.execute(text("DELETE FROM providers"))  # 清空数据，保留结构

    initialize_app_schema(engine)
    count = _raw_rows(engine, "SELECT COUNT(*) FROM providers")[0][0]
    assert count == 0, f"空表迁移后行数应为 0，实际 {count}"


# --------------------------------------------------------------------------- #
# 兜底：原生 sqlite3 校验 RENAME/DROP 语法（独立于 SQLAlchemy 编译）
# --------------------------------------------------------------------------- #
def test_raw_sqlite_rename_drop_syntax(tmp_path: Path) -> None:
    """目的：用原生 sqlite3 直接验证 RENAME/DROP 语句可执行（兜底语法校验）；潜在缺陷：低版本 SQLite 不支持 DROP COLUMN。"""
    db = tmp_path / "raw.sqlite3"
    con = sqlite3.connect(str(db))
    cur = con.cursor()
    cur.execute(
        "CREATE TABLE providers (provider_id TEXT, name TEXT, api_key_env TEXT, api_version TEXT)"
    )
    cur.execute("ALTER TABLE providers RENAME COLUMN api_key_env TO api_key")
    cur.execute("ALTER TABLE providers DROP COLUMN api_version")
    cols = [r[1] for r in cur.execute("PRAGMA table_info(providers)").fetchall()]
    assert "api_key" in cols and "api_key_env" not in cols and "api_version" not in cols
    con.close()


# --------------------------------------------------------------------------- #
# 补充：覆盖 init_schema 其余分支（日志库迁移 / NOT NULL 零值推导 / 默认参数）
# 这些分支与「列迁移」同属 schema 自愈职责，且能暴露潜在缺陷，故一并覆盖至 >80%。
# --------------------------------------------------------------------------- #
def test_ensure_model_columns_default_models_arg(tmp_path: Path) -> None:
    """目的：_ensure_model_columns 缺省 models 参数（=APP_MODELS）时仍能补列且跳过不存在的表；潜在缺陷：默认分支/跳过分支未实现。"""
    engine = _make_engine(tmp_path)
    with engine.begin() as connection:
        # 造一张缺列的 tasks 表（只含主键），缺其余 ORM 列
        connection.execute(text("DROP TABLE IF EXISTS tasks"))
        connection.execute(
            text("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, title TEXT NOT NULL)")
        )

    # 不传 models，使用默认 APP_MODELS
    with engine.begin() as connection:
        _ensure_model_columns(connection, engine)

    cols = _raw_cols(engine, "tasks")
    # 至少不应报错，且 task_id/title 保留
    assert "task_id" in cols and "title" in cols, "缺省参数补列后现有列应保留"


def test_default_literal_for_type_branches() -> None:
    """目的：_default_literal_for_type 对各类型返回正确零值字面量；潜在缺陷：NOT NULL 补列推导默认值错误导致约束失败。"""
    assert _default_literal_for_type(Integer()) == "0", "INTEGER 应为 0"
    assert _default_literal_for_type(Boolean()) == "0", "BOOLEAN 应为 0"
    assert _default_literal_for_type(Float()) == "0.0", "FLOAT 应为 0.0"
    assert _default_literal_for_type(Numeric()) == "0.0", "NUMERIC 应为 0.0"
    assert _default_literal_for_type(Text()) == "''", "TEXT 应为空串"


def test_initialize_log_schema_fresh_creates_and_sets_version(tmp_path: Path) -> None:
    """目的：initialize_log_schema 在全新库上建表并写入 user_version；潜在缺陷：首次初始化未写版本号。"""
    engine = _make_engine(tmp_path)
    initialize_log_schema(engine)

    with engine.connect() as connection:
        version = connection.execute(text("PRAGMA user_version")).scalar_one()
        from sqlalchemy import inspect as _inspect

        has_log = _inspect(connection).has_table("log_entries")
    assert has_log is True, "应创建 log_entries 表"
    assert version == 2, f"user_version 应=2，实际 {version}"


def test_initialize_log_schema_rebuild_when_legacy_version_zero(tmp_path: Path) -> None:
    """目的：user_version=0 但已存在旧 log_entries 表时按整表重建到当前版本；潜在缺陷：历史结构未重建导致列缺失。"""
    engine = _make_engine(tmp_path)
    with engine.begin() as connection:
        # 旧结构（少列的 log_entries）
        connection.execute(text("DROP TABLE IF EXISTS log_entries"))
        connection.execute(
            text("CREATE TABLE log_entries (id TEXT PRIMARY KEY, message TEXT)")
        )
        connection.execute(text("PRAGMA user_version = 0"))
        connection.execute(text("INSERT INTO log_entries VALUES ('x', 'old')"))

    initialize_log_schema(engine)

    with engine.connect() as connection:
        version = connection.execute(text("PRAGMA user_version")).scalar_one()
        cols = {c["name"] for c in inspect(connection).get_columns("log_entries")}
    # 重建为当前模型结构（含 ts/level/data_json 等），旧行被丢弃（日志库策略）
    assert version == 2, f"重建后 user_version 应=2，实际 {version}"
    assert "ts" in cols and "data_json" in cols, "重建后应为当前模型列结构"


def test_initialize_log_schema_rebuild_when_version_behind(tmp_path: Path) -> None:
    """目的：user_version 落后（<2）时重建到当前版本；潜在缺陷：版本落后未触发重建。"""
    engine = _make_engine(tmp_path)
    # 先按当前结构建好并写版本 1（落后）
    with engine.begin() as connection:
        from app.storage.model.log_model import LogEntryModel

        LogEntryModel.__table__.create(bind=connection, checkfirst=True)
        connection.execute(text("PRAGMA user_version = 1"))

    initialize_log_schema(engine)

    with engine.connect() as connection:
        version = connection.execute(text("PRAGMA user_version")).scalar_one()
    assert version == 2, f"版本落后应重建到 2，实际 {version}"
