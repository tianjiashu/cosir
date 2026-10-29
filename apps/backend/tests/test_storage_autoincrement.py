"""存储表的自增主键契约：行 id 是对外身份，删除后不得被新行复用。"""

import importlib
import pkgutil

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable

import app.storage.model as model_package
from app.storage.model.base import StorageBase

for _module in pkgutil.iter_modules(model_package.__path__):
    importlib.import_module(f"{model_package.__name__}.{_module.name}")


@pytest.mark.parametrize("table", StorageBase.metadata.tables.values(), ids=lambda t: t.name)
def test_every_table_uses_sqlite_autoincrement(table) -> None:
    """子类自定义 ``__table_args__``（索引、约束）不得让表丢掉 AUTOINCREMENT。

    回归：基类曾把 ``sqlite_autoincrement`` 放在 ``__table_args__``，声明了自身索引的表会整体
    覆盖它，SQLite 于是复用已删除的最大 id（Run、message_queue 消息等对外身份会撞 id）。
    """
    ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))
    assert "AUTOINCREMENT" in ddl


def test_deleted_max_id_is_not_reused(isolated_storage) -> None:
    """删除最大 id 的行后，新行仍拿到更大的 id。"""
    from app.storage.store_engines import main_session_factory

    with main_session_factory().begin() as session:
        session.execute(
            text(
                "INSERT INTO model_configs (config_name, base_url, api_key, model_name, "
                "context_window_k, created_at, updated_at) "
                "VALUES ('a', 'u', 'k', 'm', 1, 't', 't')"
            )
        )
        first = session.execute(text("SELECT max(id) FROM model_configs")).scalar_one()
        session.execute(text("DELETE FROM model_configs WHERE id = :id"), {"id": first})
        session.execute(
            text(
                "INSERT INTO model_configs (config_name, base_url, api_key, model_name, "
                "context_window_k, created_at, updated_at) "
                "VALUES ('b', 'u', 'k', 'm', 1, 't', 't')"
            )
        )
        second = session.execute(text("SELECT max(id) FROM model_configs")).scalar_one()
    assert second > first
