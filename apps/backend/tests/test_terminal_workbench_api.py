"""终端工作台 HTTP：shell 列表、活跃计数、用户新建 / 关闭会话与错误映射。

终端服务用替身：本文件只验证 HTTP 编排（Task 类型判定、工作区根目录、``created_by="user"``）与
``detail={code, message, retryable}`` 的状态码映射，会话生命周期由终端服务自己的测试覆盖。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import terminal_sessions_api
from app.app import app
from app.service import depends
from app.service.terminal import terminal_workbench_service
from app.tools.terminal_runtime.errors import (
    ShellResolutionError,
    TerminalSessionCapacityError,
    TerminalSessionNotFoundError,
    TerminalSessionOwnershipError,
    TerminalSessionStateError,
    TerminalWorkerUnavailableError,
)
from app.tools.terminal_runtime.shell_resolver import ShellResolver

_SHELLS = [{"kind": "zsh", "label": "zsh", "isDefault": True, "integrationSupported": True}]

pytestmark = pytest.mark.usefixtures("isolated_storage")


class _FakeTerminals:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.closed: list[tuple[str, int]] = []
        self.start_error: Exception | None = None
        self.close_error: Exception | None = None

    def active_session_count(self) -> int:
        return 3

    def start(self, **kwargs: Any) -> SimpleNamespace:
        if self.start_error is not None:
            raise self.start_error
        self.started.append(kwargs)
        return SimpleNamespace(session_id="term_new")

    def close(self, session_id: str, *, task_id: int, by: str) -> dict[str, object]:
        if self.close_error is not None:
            raise self.close_error
        self.closed.append((session_id, task_id, by))
        return {}


@pytest.fixture
def terminals(monkeypatch: pytest.MonkeyPatch) -> _FakeTerminals:
    fake = _FakeTerminals()
    monkeypatch.setattr(terminal_workbench_service, "get_terminal_session_service", lambda: fake)
    monkeypatch.setattr(terminal_sessions_api, "get_terminal_session_service", lambda: fake)
    monkeypatch.setattr(ShellResolver, "available", staticmethod(lambda: _SHELLS))
    return fake


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def workspace(tmp_path) -> Any:
    return depends.get_workspace_crud().create("ws", str(tmp_path))


def _task(workspace: Any, task_type: str = "user") -> int:
    tasks = depends.get_task_crud()
    if task_type == "delegate_task":
        parent = tasks.create(workspace.id, "parent")
        return tasks.create(
            workspace.id, "child", task_type=task_type, parent_task_id=parent.id
        ).id
    return tasks.create(workspace.id, "task", task_type=task_type).id


def test_list_shells_and_active_count(client: TestClient, terminals: _FakeTerminals) -> None:
    assert client.get("/terminal/shells").json() == {"shells": _SHELLS}
    assert client.get("/terminal/sessions/active-count").json() == {"count": 3}


@pytest.mark.parametrize("task_type", ["user", "fork"])
def test_user_creates_session_for_sidebar_task(
    client: TestClient, terminals: _FakeTerminals, workspace: Any, task_type: str
) -> None:
    task_id = _task(workspace, task_type)
    response = client.post(f"/tasks/{task_id}/terminal/sessions", json={"shell": "bash"})
    assert response.status_code == 200
    assert response.json() == {"sessionId": "term_new"}
    assert terminals.started == [
        {
            "task_id": task_id,
            "workspace_root": workspace.root_path,
            "shell": "bash",
            "created_by": "user",
            "run_id": None,
        }
    ]


def test_create_defaults_shell_to_auto(
    client: TestClient, terminals: _FakeTerminals, workspace: Any
) -> None:
    task_id = _task(workspace)
    assert client.post(f"/tasks/{task_id}/terminal/sessions", json={}).status_code == 200
    assert terminals.started[0]["shell"] == "auto"


def test_create_rejects_child_task(
    client: TestClient, terminals: _FakeTerminals, workspace: Any
) -> None:
    task_id = _task(workspace, "delegate_task")
    response = client.post(f"/tasks/{task_id}/terminal/sessions", json={"shell": "auto"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "TERMINAL_TASK_UNSUPPORTED"
    assert response.json()["detail"]["retryable"] is False
    assert terminals.started == []


def test_create_for_missing_task_is_404(client: TestClient, terminals: _FakeTerminals) -> None:
    response = client.post("/tasks/999/terminal/sessions", json={"shell": "auto"})
    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "TASK_NOT_FOUND",
        "message": "task not found",
        "retryable": False,
    }


@pytest.mark.parametrize(
    ("error", "status", "code", "retryable"),
    [
        (ShellResolutionError("no fish"), 400, "TERMINAL_SHELL_UNAVAILABLE", False),
        (TerminalSessionCapacityError("full"), 409, "TERMINAL_SESSION_CAPACITY", True),
        (TerminalSessionStateError("closing"), 409, "TERMINAL_SESSION_STATE_ERROR", False),
        (TerminalWorkerUnavailableError("gone"), 503, "TERMINAL_WORKER_UNAVAILABLE", True),
    ],
)
def test_create_error_mapping(
    client: TestClient,
    terminals: _FakeTerminals,
    workspace: Any,
    error: Exception,
    status: int,
    code: str,
    retryable: bool,
) -> None:
    terminals.start_error = error
    task_id = _task(workspace)
    response = client.post(f"/tasks/{task_id}/terminal/sessions", json={"shell": "auto"})
    assert response.status_code == status
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["retryable"] is retryable
    assert isinstance(detail["message"], str) and detail["message"]


def test_create_rejects_malformed_body(
    client: TestClient, terminals: _FakeTerminals, workspace: Any
) -> None:
    task_id = _task(workspace)
    response = client.post(f"/tasks/{task_id}/terminal/sessions", json={"shell": "auto", "x": 1})
    assert response.status_code in {400, 422}
    assert terminals.started == []


def test_close_session(client: TestClient, terminals: _FakeTerminals, workspace: Any) -> None:
    task_id = _task(workspace)
    response = client.delete(f"/tasks/{task_id}/terminal/sessions/term_a")
    assert response.status_code == 200
    assert response.json() == {}
    assert terminals.closed == [("term_a", task_id, "user")]


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (TerminalSessionNotFoundError("missing"), 404),
        (TerminalSessionOwnershipError("other task"), 404),
        (TerminalWorkerUnavailableError("gone"), 503),
    ],
)
def test_close_error_mapping(
    client: TestClient,
    terminals: _FakeTerminals,
    workspace: Any,
    error: Exception,
    status: int,
) -> None:
    terminals.close_error = error
    task_id = _task(workspace)
    response = client.delete(f"/tasks/{task_id}/terminal/sessions/term_a")
    assert response.status_code == status
    assert set(response.json()["detail"]) == {"code", "message", "retryable"}
