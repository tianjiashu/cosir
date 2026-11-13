"""目录的写入方在提交后推送失效：侧栏只经推送得知目录变化，漏一个写入方就有一类变化永远不出现。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models.task_kind import TaskType
from app.service import depends
from app.storage.crud.task_crud import TaskCrud

pytestmark = pytest.mark.usefixtures("backend_env")


class _RecordingFeed:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    def publish_catalog(self, workspace_id: int, task_type: TaskType) -> None:
        self.events.append(("catalog", workspace_id, task_type))

    def publish_task_catalog(self, task_id: int) -> None:
        self.events.append(("task_catalog", task_id))

    def publish_workspaces(self) -> None:
        self.events.append(("workspaces",))


@pytest.fixture
def feed(monkeypatch: pytest.MonkeyPatch) -> _RecordingFeed:
    recording = _RecordingFeed()
    monkeypatch.setattr(depends, "get_task_feed", lambda: recording)
    return recording


def test_workspace_and_task_writes_publish_after_commit(feed: _RecordingFeed, tmp_path: Path) -> None:
    workspaces = depends.get_workspace_service()
    tasks = depends.get_task_service()
    root = tmp_path / "ws"
    root.mkdir()

    workspace = workspaces.create_workspace("ws", str(root))
    assert feed.events == [("workspaces",)]
    workspaces.create_workspace("again", str(root))  # 复用既有记录：目录没变
    assert feed.events == [("workspaces",)]

    task = workspaces.create_task(workspace.id, "first")
    assert feed.events[-1] == ("catalog", workspace.id, TaskType.USER)

    tasks.update_title(task.id, "named")
    assert feed.events[-1] == ("task_catalog", task.id)
    assert TaskCrud().get(task.id).title == "named"

    tasks.delete_task(task.id)
    assert feed.events[-1] == ("catalog", workspace.id, TaskType.USER)

    workspaces.delete_workspace(workspace.id)
    assert feed.events[-1] == ("workspaces",)
