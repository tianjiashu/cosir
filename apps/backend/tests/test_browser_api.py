"""``GET /sessions/{id}/browser-context``：用户在面板里新开标签前取的标签上下文。

与 Agent 路径（``call_browser``）共用 ``tab_context`` 的解析；这里只验证 HTTP 形状、
按 Session 解析出工作区下载目录与 404 映射。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.app import app
from app.config.settings import Settings
from app.service import depends

pytestmark = pytest.mark.usefixtures("isolated_storage")


def test_returns_limits_and_the_workspace_download_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Settings, "BROWSER_MAX_TABS_PER_SESSION", 2)
    monkeypatch.setattr(Settings, "BROWSER_MAX_TABS", 7)
    workspace = depends.get_workspace_crud().create("ws", str(tmp_path))
    session_id = depends.get_session_crud().create(workspace.id, "session").id

    response = TestClient(app).get(f"/sessions/{session_id}/browser-context")

    assert response.status_code == 200
    assert response.json() == {
        "limits": {"per_session": 2, "total": 7},
        "download_dir": str(tmp_path.resolve() / ".cosir" / "Download"),
    }


def test_missing_session_is_404() -> None:
    response = TestClient(app).get("/sessions/999/browser-context")

    assert response.status_code == 404
