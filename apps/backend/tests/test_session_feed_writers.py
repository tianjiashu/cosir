"""目录的写入方在提交后推送失效：侧栏只经推送得知目录变化，漏一个写入方就有一类变化永远不出现。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models.session_kind import SessionType
from app.service import depends
from app.storage.crud.session_crud import SessionCrud

pytestmark = pytest.mark.usefixtures("backend_env")


class _RecordingFeed:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    def publish_catalog(self, workspace_id: int, session_type: SessionType) -> None:
        self.events.append(("catalog", workspace_id, session_type))

    def publish_session_catalog(self, session_id: int) -> None:
        self.events.append(("session_catalog", session_id))

    def publish_workspaces(self) -> None:
        self.events.append(("workspaces",))


@pytest.fixture
def feed(monkeypatch: pytest.MonkeyPatch) -> _RecordingFeed:
    recording = _RecordingFeed()
    monkeypatch.setattr(depends, "get_session_feed", lambda: recording)
    return recording


def test_workspace_and_session_writes_publish_after_commit(feed: _RecordingFeed, tmp_path: Path) -> None:
    workspaces = depends.get_workspace_service()
    sessions = depends.get_session_service()
    root = tmp_path / "ws"
    root.mkdir()

    workspace = workspaces.create_workspace("ws", str(root))
    assert feed.events == [("workspaces",)]
    workspaces.create_workspace("again", str(root))  # 复用既有记录：目录没变
    assert feed.events == [("workspaces",)]

    session = workspaces.create_session(workspace.id, "first")
    assert feed.events[-1] == ("catalog", workspace.id, SessionType.USER)

    sessions.update_title(session.id, "named")
    assert feed.events[-1] == ("session_catalog", session.id)
    assert SessionCrud().get(session.id).title == "named"
    published = len(feed.events)
    sessions.update_title(session.id, "named")  # 标题没变：不写库、不推送
    assert len(feed.events) == published

    sessions.delete_session(session.id)
    assert feed.events[-1] == ("catalog", workspace.id, SessionType.USER)

    workspaces.delete_workspace(workspace.id)
    assert feed.events[-1] == ("workspaces",)
