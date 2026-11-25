"""``GET /sessions/{id}/browser-context``：用户在面板里新开标签前取的标签上下文。

与 Agent 路径（``call_browser``）共用 ``tab_context`` 的解析；这里验证 HTTP 形状、按 Session 解析出
工作区下载目录与显示会话，以及 404 映射。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.app import app
from app.config.settings import Settings
from app.models.session.session_kind import SessionType
from app.service import depends
from tests.http_test_support import TRUSTED_HEADERS
from tests.session_extra_fixtures import fork_extra

pytestmark = pytest.mark.usefixtures("isolated_storage")


def test_returns_limits_and_the_workspace_download_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Settings, "BROWSER_MAX_TABS_PER_SESSION", 2)
    monkeypatch.setattr(Settings, "BROWSER_MAX_TABS", 7)
    workspace = depends.get_workspace_crud().create("ws", str(tmp_path))
    session_id = depends.get_session_crud().create(workspace.id, "session").id

    response = TestClient(app, headers=TRUSTED_HEADERS).get(
        f"/sessions/{session_id}/browser-context"
    )

    assert response.status_code == 200
    assert response.json() == {
        "limits": {"per_session": 2, "total": 7},
        "download_dir": str(tmp_path.resolve() / ".cosir" / "Download"),
        "display_session_id": session_id,
    }


def test_missing_session_is_404() -> None:
    response = TestClient(app, headers=TRUSTED_HEADERS).get("/sessions/999/browser-context")

    assert response.status_code == 404


# 目的：子 Agent 的标签算在父会话页上，分叉的不算；潜在缺陷：子 Agent 在用浏览器时父会话入口不动，
# 或分叉的标签被算进来源会话。
def test_display_session_is_the_nearest_user_facing_owner(tmp_path: Path) -> None:
    workspace = depends.get_workspace_crud().create("ws", str(tmp_path))
    sessions = depends.get_session_crud()
    root = sessions.create(workspace.id, "root").id
    child = sessions.create(workspace.id, "child", SessionType.DELEGATED, parent_session_id=root).id
    grandchild = sessions.create(workspace.id, "grandchild", SessionType.DELEGATED, parent_session_id=child).id
    fork = sessions.create(workspace.id, "fork", SessionType.AGENT_FORK, extra=fork_extra(root)).id
    client = TestClient(app, headers=TRUSTED_HEADERS)

    displayed = {
        name: client.get(f"/sessions/{session_id}/browser-context").json()["display_session_id"]
        for name, session_id in {"child": child, "grandchild": grandchild, "fork": fork}.items()
    }

    assert displayed == {"child": root, "grandchild": root, "fork": fork}
