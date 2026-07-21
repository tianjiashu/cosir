"""init_schema 日志库迁移 + TurnService 编排方法的补充测试。

聚焦本次改动相关模块中尚未覆盖、却属于行为契约的部分：
- initialize_log_schema 的“首次建表/版本重建”分支（日志库策略）；
- _default_literal_for_type 的零值推导（缺列迁移时对 NOT NULL 无默认列的安全兜底）；
- TurnService.create_turn / update_turn_status / get_latest_turn 的委托与编排。

导入前置：仓库存在坏导入链，需在导入 app 模块之前注入 app.storage.crud.log 占位。
"""

import sys
from pathlib import Path
import tempfile

sys.path.insert(0, ".")

from tests.conftest_stub_log import install_log_crud_stub

install_log_crud_stub()

import pytest
from sqlalchemy import text

from app.config.settings import BackendSettings
from app.service.task.task_service import TaskService
from app.service.task.turn_service import TurnService
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.turn_crud import TurnCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.init_schema import (
    _default_literal_for_type,
    initialize_log_schema,
)
from app.storage.store_engines import close_storage, init_storage


@pytest.fixture()
def storage_settings():
    tmp = Path(tempfile.mkdtemp(prefix="coding_agent_schema_"))
    settings = BackendSettings(
        project_root=tmp,
        log_dir=tmp / "logs",
        database_file=tmp / "app.sqlite3",
        log_database_file=tmp / "logs.sqlite3",
        checkpoint_file=tmp / "langgraph_checkpoints.sqlite",
    )
    init_storage(settings)
    yield settings
    close_storage()


# 测试目的：initialize_log_schema 对空日志库首次建表并写入 user_version=2。缺陷：日志库初始化遗漏或版本错。
def test_initialize_log_schema_creates_and_sets_version(storage_settings):
    from app.storage.store_engines import _engine_cache

    engine = _engine_cache.get(storage_settings.log_database_file)
    initialize_log_schema(engine)
    with engine.connect() as conn:
        version = int(conn.execute(text("PRAGMA user_version")).scalar_one())
        assert version == 2
        # log_entries 表应已存在。
        assert conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='log_entries'")
        ).first() is not None


# 测试目的：initialize_log_schema 对“版本落后 + 无版本号旧表”触发整表重建到当前版本。缺陷：落后库不重建导致结构错配。
def test_initialize_log_schema_rebuilds_when_version_zero_with_old_table(storage_settings):
    from app.storage.store_engines import _engine_cache

    engine = _engine_cache.get(storage_settings.log_database_file)
    # 构造一个“无 user_version、但已有旧 log_entries”的存量库。
    with engine.begin() as conn:
        conn.execute(text("PRAGMA user_version = 0"))
        conn.execute(text("DROP TABLE IF EXISTS log_entries"))
        conn.execute(
            text("CREATE TABLE log_entries (id TEXT PRIMARY KEY, old_col TEXT)")
        )
    initialize_log_schema(engine)
    with engine.connect() as conn:
        version = int(conn.execute(text("PRAGMA user_version")).scalar_one())
        assert version == 2
        # 旧结构被整表重建：新表不应再有 old_col，且能正常插入新结构行（这里只验证列已更新）。
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(log_entries)")).all()}
        assert "old_col" not in cols


# 测试目的：_default_literal_for_type 为无默认 NOT NULL 列推导安全零值（整型/浮点/文本）。缺陷：零值推导错导致 ALTER 失败。
def test_default_literal_for_type_derives_zero_values():
    from sqlalchemy import Boolean, Float, Integer, Text

    assert _default_literal_for_type(Integer()) == "0"
    assert _default_literal_for_type(Boolean()) == "0"
    assert _default_literal_for_type(Float()) == "0.0"
    assert _default_literal_for_type(Text()) == "''"


def _services():
    task_crud = TaskCrud()
    turn_crud = TurnCrud()
    workspace_crud = WorkspaceCrud()
    task_service = TaskService(task_crud, turn_crud, workspace_crud)
    turn_service = TurnService(task_crud, turn_crud)
    return task_crud, turn_crud, task_service, turn_service


# 测试目的：TurnService.create_turn 创建 pending turn 并同步更新 task 最新轮指针。缺陷：创建后 task 指针未更新。
def test_turn_service_create_turn_updates_task_pointer(storage_settings):
    _, turn_crud, task_service, turn_service = _services()
    task = task_service.create_task("svc-create", "open")
    turn = turn_service.create_turn(task.task_id, "fresh input")

    assert turn.status == "pending"
    assert turn.input_text == "fresh input"
    # task 的最新轮指针应指向新 turn。
    assert task_service.get_task(task.task_id).latest_turn_id == turn.turn_id


# 测试目的：TurnService.create_turn 拒绝空白输入（ValueError）。缺陷：空输入被建为 turn。
def test_turn_service_create_turn_rejects_blank(storage_settings):
    _, _, task_service, turn_service = _services()
    task = task_service.create_task("svc-blank", "open")
    with pytest.raises(ValueError):
        turn_service.create_turn(task.task_id, "   ")


# 测试目的：TurnService.update_turn_status 委托到 crud 并返回更新后记录。缺陷：委托层错接/状态未更新。
def test_turn_service_update_turn_status_delegates(storage_settings):
    _, turn_crud, task_service, turn_service = _services()
    task = task_service.create_task("svc-status", "open")
    turn = turn_crud.get_latest_turn(task.task_id)

    updated = turn_service.update_turn_status(turn.turn_id, "failed", end_reason="client_disconnected")
    assert updated.status == "failed"
    assert updated.end_reason == "client_disconnected"
    assert turn_service.has_turn_status(turn.turn_id, "failed") is True


# 测试目的：TurnService.get_latest_turn 返回 task 当前最新轮。缺陷：取错轮次。
def test_turn_service_get_latest_turn(storage_settings):
    _, turn_crud, task_service, turn_service = _services()
    task = task_service.create_task("svc-latest2", "open")
    turn_crud.create(task.task_id, "a", "pending")
    t2 = turn_crud.create(task.task_id, "b", "pending")

    assert turn_service.get_latest_turn(task.task_id).turn_id == t2.turn_id
    assert turn_service.get_latest_turn(task.task_id).turn_id == t2.turn_id
