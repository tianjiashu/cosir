"""回归测试：create_task 必须先建 task 再建首 turn，避免外键约束失败。

``turns.task_id`` 外键指向 ``tasks.task_id``，``tasks.workspace_id`` 外键指向
``workspaces.workspace_id``。若插入顺序颠倒，会触发 IntegrityError(500)。
本测试通过完整导入图(经 ``app.main``)初始化存储，复用库中已有 workspace 创建任务，
断言 task 与首 turn 均落库且 ``latest_turn_id`` 指向真实 turn。
"""

from fastapi.testclient import TestClient

from app.api.app import app as fastapi_app
from app.config.logging import install_logging_for_current_process
from app.config.settings import Settings
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.store_engines import init_storage


def test_create_task_foreign_key_order():
    """创建任务应成功写入 task 与首 turn，且 latest_turn_id 一致。"""
    Settings.load()
    init_storage()
    install_logging_for_current_process(
        log_dir=Settings.LOG_DIR,
        log_database_file=Settings.LOG_DATABASE_FILE,
        sqlite_logging_enabled=Settings.SQLITE_LOGGING_ENABLED,
    )
    # 复用库中已有 workspace，避免依赖外部新建 workspace 的可见性时序。
    workspace_id = WorkspaceCrud().list_all()[0].workspace_id

    client = TestClient(fastapi_app)
    resp = client.post(
        f"/workspaces/{workspace_id}/tasks",
        json={"text": "hello", "workspace_id": workspace_id, "agent_id": "developer"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["latest_turn_id"] is not None

    turns = client.get(f"/tasks/{body['task_id']}/turns").json()
    assert len(turns) == 1
    assert turns[0]["input_text"] == "hello"
    assert turns[0]["turn_id"] == body["latest_turn_id"]
