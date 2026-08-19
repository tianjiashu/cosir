"""``init_schema`` 列迁移逻辑测试。

验证 ``_ensure_model_columns`` 对存量旧库的演进：
- providers 表的 ``api_key_env`` 旧列重命名为 ``api_key``；
- 模型新增列能被 ``ADD COLUMN`` 补齐。

这些迁移保证「用户机旧库启动自愈」，不再因 ORM 字段漂移导致 500。
"""

import sqlite3
from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect, text

from app.storage.init_schema import (
    _COLUMN_RENAME_MAP,
    _ensure_model_columns,
    initialize_app_schema,
)


def _make_engine(tmp_path: Path) -> Engine:
    """创建一个临时 SQLite 引擎用于迁移测试。"""
    db_path = tmp_path / "app.sqlite3"
    return create_engine(f"sqlite:///{db_path}")


def _create_legacy_providers(connection) -> None:
    """手工造一张「用户机旧库」风格的 providers 表：含 api_key_env、api_version，缺 api_key。"""
    connection.execute(text("DROP TABLE IF EXISTS providers"))
    connection.execute(
        text(
            "CREATE TABLE providers ("
            "provider_id TEXT PRIMARY KEY, "
            "name TEXT NOT NULL, "
            "type TEXT NOT NULL, "
            "base_url TEXT, "
            "api_key_env TEXT, "
            "enabled INTEGER NOT NULL DEFAULT 1, "
            "api_version TEXT, "
            "created_at TEXT"
            ")"
        )
    )
    connection.execute(
        text(
            "INSERT INTO providers (provider_id, name, type, api_key_env, api_version) "
            "VALUES ('p1', 'DeepSeek', 'deepseek', 'sk-xxx', 'v1')"
        )
    )


def test_legacy_providers_migrated_on_init(tmp_path: Path) -> None:
    """旧库 providers 表经 initialize_app_schema 后应把 api_key_env 重命名为 api_key。"""
    engine = _make_engine(tmp_path)
    with engine.begin() as connection:
        _create_legacy_providers(connection)

    initialize_app_schema(engine)

    with engine.connect() as connection:
        inspector = inspect(connection)
        columns = {col["name"] for col in inspector.get_columns("providers")}
        assert "api_key" in columns, "api_key_env 应被重命名为 api_key"
        assert "api_key_env" not in columns, "旧列名应已消失"
        # 存量数据应保留且随重命名迁移到新列
        row = connection.execute(
            text("SELECT name, api_key FROM providers WHERE provider_id = 'p1'")
        ).fetchone()
        assert row is not None
        assert row[0] == "DeepSeek"
        assert row[1] == "sk-xxx"


def test_rename_map_covers_api_key_env() -> None:
    """_COLUMN_RENAME_MAP 应登记 providers 表的 api_key_env → api_key。"""
    assert _COLUMN_RENAME_MAP.get("providers", {}).get("api_key_env") == "api_key"


def test_ensure_model_columns_adds_missing(tmp_path: Path) -> None:
    """_ensure_model_columns 能为已有表补齐 ORM 声明但库里缺失的新列（不删现有列）。"""
    engine = _make_engine(tmp_path)
    with engine.begin() as connection:
        # 造一张缺列的 providers（只有 provider_id / name，缺 type / api_key 等）
        connection.execute(text("DROP TABLE IF EXISTS providers"))
        connection.execute(
            text(
                "CREATE TABLE providers ("
                "provider_id TEXT PRIMARY KEY, "
                "name TEXT NOT NULL"
                ")"
            )
        )
    with engine.begin() as connection:
        # 仅对 providers 做列迁移
        from app.storage.model.provider_model import ProviderModel

        _ensure_model_columns(connection, engine, (ProviderModel,))

    with engine.connect() as connection:
        columns = {col["name"] for col in inspect(connection).get_columns("providers")}
        assert "type" in columns, "缺失列 type 应被补齐"
        assert "api_key" in columns, "缺失列 api_key 应被补齐"
        assert "name" in columns, "现有列不应被删除"


def test_direct_sqlite_compat(tmp_path: Path) -> None:
    """直接用 sqlite3 验证 RENAME/DROP 语句在 SQLite 上可执行（兜底语法校验）。"""
    db = tmp_path / "raw.sqlite3"
    con = sqlite3.connect(str(db))
    cur = con.cursor()
    cur.execute(
        "CREATE TABLE providers (provider_id TEXT, name TEXT, api_key_env TEXT, api_version TEXT)"
    )
    cur.execute("ALTER TABLE providers RENAME COLUMN api_key_env TO api_key")
    cur.execute("ALTER TABLE providers DROP COLUMN api_version")
    cols = [r[1] for r in cur.execute("PRAGMA table_info(providers)").fetchall()]
    assert "api_key" in cols
    assert "api_key_env" not in cols
    assert "api_version" not in cols
    con.close()
