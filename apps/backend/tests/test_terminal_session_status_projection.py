"""snapshot ``terminal_sessions`` 的契约校验与投影：重建整表填入、变化通知推送 set mutation。

终端会话是进程内事实：重建与刷新只读 ``TerminalSessionService.project_task_sessions``，不回写
工具卡片，也不为未物化 / 已删除的 Task 物化 space。
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest

from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    empty_snapshot,
    validate_snapshot,
)
from app.assistant_transport.stream import TransportFrame
from app.service import depends
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

pytestmark = pytest.mark.usefixtures("isolated_storage")


def _entry(session_id: str = "term_1", **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "sessionId": session_id,
        "label": "终端 1",
        "shellKind": "zsh",
        "createdBy": "user",
        "status": "running",
        "endReason": None,
        "exitCode": None,
        "integration": "ready",
        "inputHolder": None,
        "commandRunning": False,
        "handoff": None,
        "createdAt": "2026-10-27T00:00:00+00:00",
    }
    entry.update(overrides)
    return entry


class _FakeTerminals:
    """只实现投影用到的 ``project_task_sessions``；按 task 返回可变列表。"""

    def __init__(self) -> None:
        self.sessions: dict[int, list[dict[str, Any]]] = {}
        self.calls: list[int] = []

    def project_task_sessions(self, task_id: int) -> list[dict[str, Any]]:
        self.calls.append(task_id)
        return copy.deepcopy(self.sessions.get(task_id, []))


@pytest.fixture
def terminals(monkeypatch: pytest.MonkeyPatch) -> _FakeTerminals:
    fake = _FakeTerminals()

    def accessor() -> _FakeTerminals:
        return fake

    # 依赖关闭入口会探测 lru_cache 状态；替身声明「从未构造」，关闭时不调用 shutdown。
    accessor.cache_info = lambda: SimpleNamespace(currsize=0)  # type: ignore[attr-defined]
    accessor.cache_clear = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(depends, "get_terminal_session_service", accessor)
    return fake


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[TransportFrame]:
    frames: list[TransportFrame] = []
    original = ConversationTaskStateService._publish

    def record(self: ConversationTaskStateService, change: TransportFrame) -> None:
        frames.append(change)
        original(self, change)

    monkeypatch.setattr(ConversationTaskStateService, "_publish", record)
    return frames


@pytest.fixture
def task_id(tmp_path) -> int:
    workspace = depends.get_workspace_crud().create("ws", str(tmp_path))
    return depends.get_task_crud().create(workspace.id, "终端任务").id


# ── 契约校验 ─────────────────────────────────────────────────────────────


def test_empty_snapshot_carries_required_terminal_sessions() -> None:
    state = empty_snapshot()
    assert state["terminal_sessions"] == []
    validate_snapshot(state)


def test_snapshot_without_terminal_sessions_is_rejected() -> None:
    state: dict[str, Any] = dict(empty_snapshot())
    del state["terminal_sessions"]
    with pytest.raises(ValueError):
        validate_snapshot(state)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "entry",
    [
        _entry(status="interrupted"),
        _entry(createdBy="system"),
        _entry(integration="maybe"),
        _entry(inputHolder="both"),
        _entry(exitCode=True),
        _entry(handoff={"reason": "x", "extra": 1}),
        _entry(handoff="x"),
        _entry(sessionId=""),
        {**_entry(), "extra": 1},
        {k: v for k, v in _entry().items() if k != "createdAt"},
    ],
)
def test_invalid_terminal_session_entries_are_rejected(entry: dict[str, Any]) -> None:
    state = empty_snapshot()
    state["terminal_sessions"] = [entry]  # type: ignore[list-item]
    with pytest.raises(ValueError):
        validate_snapshot(state)


def test_valid_entries_including_handoff_and_exit_pass() -> None:
    state = empty_snapshot()
    state["terminal_sessions"] = [  # type: ignore[list-item]
        _entry("a", inputHolder="agent", handoff={"reason": "请输入密码"}, createdBy="agent"),
        _entry("b", status="exited", endReason="shell_exited", exitCode=0, integration="off"),
    ]
    validate_snapshot(state)


def test_duplicate_session_ids_are_rejected() -> None:
    state = empty_snapshot()
    state["terminal_sessions"] = [_entry("a"), _entry("a")]  # type: ignore[list-item]
    with pytest.raises(ValueError):
        validate_snapshot(state)


# ── 投影 ─────────────────────────────────────────────────────────────────


def test_rebuild_fills_terminal_sessions_from_service(
    terminals: _FakeTerminals, task_id: int
) -> None:
    terminals.sessions[task_id] = [_entry()]
    state = ConversationTaskStateService().get_state(task_id)
    assert state["terminal_sessions"] == [_entry()]


def test_refresh_publishes_set_mutation_only_on_change(
    terminals: _FakeTerminals, published: list[TransportFrame], task_id: int
) -> None:
    service = ConversationTaskStateService()
    service.get_state(task_id)
    published.clear()

    terminals.sessions[task_id] = [_entry()]
    service.refresh_terminal_sessions(task_id)
    assert len(published) == 1
    frame = published[0]
    assert frame.kind == "mutation"
    assert [(m.kind, m.path, m.value) for m in frame.mutations] == [
        ("set", ("terminal_sessions",), [_entry()])
    ]
    assert service.get_state(task_id)["terminal_sessions"] == [_entry()]

    service.refresh_terminal_sessions(task_id)
    assert len(published) == 1, "列表未变化不应再发 mutation"

    terminals.sessions[task_id] = [_entry(status="exited", exitCode=0, endReason="shell_exited")]
    service.refresh_terminal_sessions(task_id)
    assert len(published) == 2
    assert service.get_state(task_id)["terminal_sessions"][0]["status"] == "exited"


def test_refresh_skips_unmaterialized_task_without_creating_space(
    terminals: _FakeTerminals, published: list[TransportFrame], task_id: int
) -> None:
    terminals.sessions[task_id] = [_entry()]
    ConversationTaskStateService().refresh_terminal_sessions(task_id)
    assert task_runtime_spaces.get(task_id) is None
    assert published == []
    assert terminals.calls == []


def test_refresh_skips_space_without_snapshot(
    terminals: _FakeTerminals, published: list[TransportFrame], task_id: int
) -> None:
    task_runtime_spaces.get_or_create(task_id)
    ConversationTaskStateService().refresh_terminal_sessions(task_id)
    assert published == []
    assert terminals.calls == []


def test_refresh_after_task_deleted_is_silent(
    terminals: _FakeTerminals, published: list[TransportFrame], task_id: int
) -> None:
    service = ConversationTaskStateService()
    service.get_state(task_id)
    service.mark_task_deleted(task_id)
    published.clear()
    terminals.sessions[task_id] = [_entry()]
    service.refresh_terminal_sessions(task_id)
    assert published == []
    # 从未物化的 Task 收到通知也不抛错。
    service.refresh_terminal_sessions(task_id + 1000)


def test_snapshot_read_rejects_entries_violating_contract(
    terminals: _FakeTerminals, task_id: int
) -> None:
    """契约校验统一在 ``validate_snapshot``：刷新不重复校验，违约条目在读取快照时暴露。"""

    service = ConversationTaskStateService()
    service.get_state(task_id)
    terminals.sessions[task_id] = [_entry(status="bogus")]
    service.refresh_terminal_sessions(task_id)
    with pytest.raises(ValueError):
        service.get_state(task_id)
