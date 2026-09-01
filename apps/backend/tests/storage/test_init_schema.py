"""init_schema 的单元测试。

验证 ``initialize_app_schema`` 按 ``APP_MODELS`` 创建业务表，并对已存在表补齐列与索引。
"""

from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect

from app.storage.engine_cache import create_sqlite_engine
from app.storage.init_schema import initialize_app_schema


@pytest.fixture
def app_engine(tmp_path: Path) -> Engine:
    """返回一个临时文件 SQLite 引擎，用于测试 schema 初始化。"""

    db_path = tmp_path / "app.sqlite3"
    return create_sqlite_engine(db_path)


def test_initialize_app_schema_creates_provider_and_model_tables(
    app_engine: Engine,
) -> None:
    """providers 与 models 表应作为 ``APP_MODELS`` 的一部分被创建。"""

    initialize_app_schema(app_engine)

    with app_engine.connect() as connection:
        table_names = inspect(connection).get_table_names()

    assert "providers" in table_names
    assert "models" in table_names
    assert "conversation_commands" in table_names


def test_initialize_app_schema_is_idempotent(app_engine: Engine) -> None:
    """多次初始化同一引擎不应报错，且表仍然存在。"""

    initialize_app_schema(app_engine)
    initialize_app_schema(app_engine)

    with app_engine.connect() as connection:
        table_names = inspect(connection).get_table_names()

    assert "providers" in table_names
    assert "models" in table_names
    assert "conversation_commands" in table_names


def test_initialize_app_schema_creates_command_idempotency_index(app_engine: Engine) -> None:
    """命令表必须按 task_id + command_id 建立唯一幂等边界。"""

    initialize_app_schema(app_engine)

    with app_engine.connect() as connection:
        indexes = inspect(connection).get_indexes("conversation_commands")

    index = next(
        item for item in indexes if item["name"] == "uq_conversation_commands_task_command"
    )
    assert index["unique"] == 1
    assert index["column_names"] == ["task_id", "command_id"]
