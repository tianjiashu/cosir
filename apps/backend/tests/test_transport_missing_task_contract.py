"""Task 级命令对「Task 不存在」的统一契约：一律 404，不是 500 也不是 409。

前端会在 Task 刚被删除、界面还留着旧卡片或排队消息时发出这些命令（看板停止、终端卡片停止、
队列里的立即发送与删除）。``cancel_tool_call`` 曾绕开 ``_accept`` 让 ``KeyError`` 冒成 500；
排队命令曾把「Task 不存在」说成 ``MESSAGE_CONSUMED``。
"""

import logging

import pytest
from fastapi.testclient import TestClient

from app.app import app
from app.service import depends
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

pytestmark = pytest.mark.usefixtures("backend_env")

_MISSING = 987654


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", f"/tasks/{_MISSING}/tool-calls/call-x/cancel"),
        ("post", f"/tasks/{_MISSING}/tool-calls/call-x/background"),
        ("delete", f"/tasks/{_MISSING}/queued-messages/1"),
        ("post", f"/tasks/{_MISSING}/queued-messages/1/send-now"),
        ("post", f"/tasks/{_MISSING}/resume"),
        ("post", f"/tasks/{_MISSING}/cancel"),
    ],
)
def test_commands_on_missing_task_are_404(method: str, path: str) -> None:
    response = getattr(_client(), method)(path)

    assert response.status_code == 404
    assert task_runtime_spaces.get(_MISSING) is None


def test_cancel_tool_call_without_target_is_structured_409(tmp_path) -> None:
    workspace = depends.get_workspace_crud().create("w", str(tmp_path))
    task = depends.get_task_crud().create(workspace.id, "t")

    response = _client().post(f"/tasks/{task.id}/tool-calls/call-x/cancel")

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == "TOOL_CALL_NOT_CANCELLABLE"


def test_queued_refresh_on_deleted_task_is_silent(tmp_path, caplog) -> None:
    """删除与排队命令交错时，迟到的排队刷新静默返回：不报异常、不物化 space。"""
    workspace = depends.get_workspace_crud().create("w", str(tmp_path))
    task = depends.get_task_crud().create(workspace.id, "t")
    task_runtime_spaces.mark_deleted(task.id)
    caplog.set_level(logging.INFO)

    depends.get_conversation_task_state_service().refresh_queued_messages(task.id)

    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert task_runtime_spaces.get(task.id) is None
