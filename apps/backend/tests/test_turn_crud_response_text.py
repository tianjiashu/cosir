"""针对 `response_text` 落库链路的 DB 集成测试（重点新增）。

覆盖：
- ``init_storage`` 指向临时 SQLite 后，``TurnCrud.create`` 写入的 turn ``response_text`` 为 None；
- ``TurnCrud.update_response`` 后再次 ``get`` 读到的 ``response_text`` 与 ``to_dict`` 一致；
- 通过 ``TaskService.create_task`` 建立 workspace→task→turn 链路（turn 表有 task 外键）；
- 既有库缺 ``response_text`` 列时，``init_storage`` 的 ``initialize_app_schema`` 能 ALTER 补齐。

导入前置：仓库存在坏导入链，需在导入 app 模块之前注入 app.storage.crud.log 占位。
"""

import sys
import tempfile
from pathlib import Path

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
from app.storage.store_engines import close_storage, init_storage


@pytest.fixture()
def storage_settings():
    """指向临时目录的 BackendSettings，每个用例独立 DB 文件。"""

    tmp = Path(tempfile.mkdtemp(prefix="coding_agent_test_"))
    db = tmp / "app.sqlite3"
    logs = tmp / "logs.sqlite3"
    ckpt = tmp / "langgraph_checkpoints.sqlite"
    settings = BackendSettings(
        project_root=tmp,
        log_dir=tmp / "logs",
        database_file=db,
        log_database_file=logs,
        checkpoint_file=ckpt,
    )
    init_storage(settings)
    yield settings
    close_storage()


def _build_services():
    """用进程内已初始化的共享 session 工厂构造真实 CRUD/Service（无网络、无 LLM）。"""

    task_crud = TaskCrud()
    turn_crud = TurnCrud()
    from app.storage.crud.workspace_crud import WorkspaceCrud

    workspace_crud = WorkspaceCrud()
    task_service = TaskService(task_crud, turn_crud, workspace_crud)
    turn_service = TurnService(task_crud, turn_crud)
    return task_crud, turn_crud, task_service, turn_service


# 测试目的：TurnCrud.create 写入后 response_text 必须为 None（落库契约）。缺陷：创建时误填 reply。
def test_turn_crud_create_response_text_is_none(storage_settings):
    _, turn_crud, task_service, _ = _build_services()
    task = task_service.create_task("hello world", "open")
    turn = turn_crud.create(task.task_id, "user input", "pending")

    assert turn.response_text is None
    refetched = turn_crud.get(turn.turn_id)
    assert refetched.response_text is None
    assert refetched.to_dict()["response_text"] is None


# 测试目的：update_response 后再次 get 读到的 response_text 与入参一致，to_dict 也含该值。缺陷：update 未落库或字段映射错。
def test_turn_crud_update_response_roundtrip(storage_settings):
    _, turn_crud, task_service, _ = _build_services()
    task = task_service.create_task("hello again", "open")
    turn = turn_crud.create(task.task_id, "user input", "pending")

    updated = turn_crud.update_response(turn.turn_id, "this is the agent reply")
    assert updated.response_text == "this is the agent reply"

    refetched = turn_crud.get(turn.turn_id)
    assert refetched.response_text == "this is the agent reply"
    assert refetched.to_dict()["response_text"] == "this is the agent reply"


# 测试目的：TurnService.update_turn_response 委托到 TurnCrud 后，DB 中 response_text 真实更新。缺陷：委托层未写库或写错表。
def test_turn_service_update_turn_response_persists(storage_settings):
    _, turn_crud, task_service, turn_service = _build_services()
    task = task_service.create_task("svc path", "open")
    turn = turn_crud.create(task.task_id, "user input", "pending")

    turn_service.update_turn_response(turn.turn_id, "svc reply")
    assert turn_crud.get(turn.turn_id).response_text == "svc reply"


# 测试目的：update_response 传入 None 应当清空（覆盖“极少用”的清空分支）。缺陷：None 未覆盖旧值导致残留。
def test_turn_crud_update_response_to_none_clears(storage_settings):
    _, turn_crud, task_service, _ = _build_services()
    task = task_service.create_task("clear path", "open")
    turn = turn_crud.create(task.task_id, "user input", "pending")

    turn_crud.update_response(turn.turn_id, "some reply")
    assert turn_crud.get(turn.turn_id).response_text == "some reply"
    turn_crud.update_response(turn.turn_id, None)
    assert turn_crud.get(turn.turn_id).response_text is None


# 测试目的：response_text 支持多行/特殊字符文本，且往返不变。缺陷：序列化转义丢失换行。
def test_turn_crud_update_response_multiline(storage_settings):
    _, turn_crud, task_service, _ = _build_services()
    task = task_service.create_task("multiline", "open")
    turn = turn_crud.create(task.task_id, "user input", "pending")

    payload = "line1\nline2\twith tab\n\"quoted\" and 中文"
    turn_crud.update_response(turn.turn_id, payload)
    assert turn_crud.get(turn.turn_id).response_text == payload


# 测试目的：构造缺 response_text 列的老 turns 表后，initialize_app_schema 必须 ALTER 补齐该列，否则历史库升级失败。缺陷：迁移逻辑遗漏新列导致后续读写缺列报错。
def test_init_schema_alters_missing_response_text_column(storage_settings):
    from app.storage.store_engines import _engine_cache
    from app.storage.init_schema import initialize_app_schema

    # 用已初始化的主库引擎重建一个“老结构” turns 表：故意去掉 response_text 列。
    settings = storage_settings
    engine = _engine_cache.get(settings.database_file)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS turns"))
        # 模拟“缺 response_text 列”的老库结构。注意：不声明外键，避免老数据
        # 因 tasks 表中无对应行而触发 FK 约束（这不是本次迁移要测的点）。
        conn.execute(
            text(
                "CREATE TABLE turns ("
                "turn_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
                "input_text TEXT NOT NULL, status TEXT NOT NULL, "
                "end_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
        )
        # 写入一行老数据，确认迁移是“加列不删数据”。
        conn.execute(
            text(
                "INSERT INTO turns (turn_id, task_id, input_text, status, created_at, updated_at) "
                "VALUES ('old-turn', 'old-task', 'hi', 'completed', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
        )

    # 重新触发 schema 迁移（不重建 tasks/workspaces）。
    initialize_app_schema(engine)

    # 直接用同一引擎校验迁移结果（避免触碰已初始化的 session 工厂状态）。
    with engine.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(turns)")).all()}
        assert "response_text" in cols
        # 老数据仍在，且新列默认值为 NULL。
        row = conn.execute(
            text("SELECT turn_id, response_text FROM turns WHERE turn_id='old-turn'")
        ).one()
        assert row[0] == "old-turn"
        assert row[1] is None
