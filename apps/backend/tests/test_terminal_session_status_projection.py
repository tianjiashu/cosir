"""snapshot ``terminal_sessions`` 的契约校验与投影：重建整表填入、变化通知推送 set mutation。

终端会话是进程内事实：重建与刷新只读 ``TerminalSessionService.session_runtimes`` 各会话的 ``snapshot()``，不回写
工具卡片，也不为未物化 / 已删除的 Session 物化 space。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.assistant_transport.service.conversation_state_service import (
    ConversationStateService,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    empty_snapshot,
    validate_snapshot,
)
from app.assistant_transport.stream import TransportFrame
from app.service import depends
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces
from app.tools.terminal_runtime.session.session_runtime import TerminalSessionSnapshot

pytestmark = pytest.mark.usefixtures("isolated_storage", "empty_tool_registry")


def _entry(terminal_session_id: str = "term_1", **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "terminalSessionId": terminal_session_id,
        "shellKind": "zsh",
        "status": "running",
        "endReason": None,
        "exitCode": None,
        "integration": "ready",
        "commandRunning": False,
        "createdAt": "2026-10-27T00:00:00+00:00",
    }
    entry.update(overrides)
    return entry


class _FakeRuntime:
    """按 Transport 条目反推会话快照，让投影经生产映射 ``to_terminal_session`` 还原同一条目。"""

    def __init__(self, entry: dict[str, Any]) -> None:
        self._entry = entry

    def snapshot(self) -> TerminalSessionSnapshot:
        entry = self._entry
        return TerminalSessionSnapshot(
            terminal_session_id=entry["terminalSessionId"],
            shell_kind=entry["shellKind"],
            created_at=datetime.fromisoformat(entry["createdAt"]),
            status=entry["status"],
            end_reason=entry["endReason"],
            exit_code=entry["exitCode"],
            integration=entry["integration"],
            command_running=entry["commandRunning"],
            summary="",
        )


class _FakeTerminals:
    """只实现投影用到的 ``session_runtimes``；``sessions`` 按 session 保存可变条目列表。"""

    def __init__(self) -> None:
        self.sessions: dict[int, list[dict[str, Any]]] = {}
        self.calls: list[int] = []

    def session_runtimes(self, session_id: int) -> list[_FakeRuntime]:
        self.calls.append(session_id)
        return [_FakeRuntime(entry) for entry in self.sessions.get(session_id, [])]


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
    original = ConversationStateService._publish

    def record(self: ConversationStateService, change: TransportFrame) -> None:
        frames.append(change)
        original(self, change)

    monkeypatch.setattr(ConversationStateService, "_publish", record)
    return frames


@pytest.fixture
def session_id(tmp_path) -> int:
    workspace = depends.get_workspace_crud().create("ws", str(tmp_path))
    return depends.get_session_crud().create(workspace.id, "终端会话").id


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
        _entry(shellKind=1),
        _entry(integration="maybe"),
        _entry(commandRunning="yes"),
        _entry(exitCode=True),
        {**_entry(), "handoff": None},
        _entry(terminalSessionId=""),
        {**_entry(), "extra": 1},
        {k: v for k, v in _entry().items() if k != "createdAt"},
    ],
)
def test_invalid_terminal_session_entries_are_rejected(entry: dict[str, Any]) -> None:
    state = empty_snapshot()
    state["terminal_sessions"] = [entry]  # type: ignore[list-item]
    with pytest.raises(ValueError):
        validate_snapshot(state)


def test_valid_entries_including_exit_pass() -> None:
    state = empty_snapshot()
    state["terminal_sessions"] = [  # type: ignore[list-item]
        _entry("a"),
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
    terminals: _FakeTerminals, session_id: int
) -> None:
    terminals.sessions[session_id] = [_entry()]
    state = ConversationStateService().get_state(session_id)
    assert state["terminal_sessions"] == [_entry()]


def test_refresh_publishes_set_mutation_only_on_change(
    terminals: _FakeTerminals, published: list[TransportFrame], session_id: int
) -> None:
    service = ConversationStateService()
    service.get_state(session_id)
    published.clear()

    terminals.sessions[session_id] = [_entry()]
    service.refresh_terminal_sessions(session_id)
    assert len(published) == 1
    frame = published[0]
    assert frame.kind == "mutation"
    assert [(m.kind, m.path, m.value) for m in frame.mutations] == [
        ("set", ("terminal_sessions",), [_entry()])
    ]
    assert service.get_state(session_id)["terminal_sessions"] == [_entry()]

    service.refresh_terminal_sessions(session_id)
    assert len(published) == 1, "列表未变化不应再发 mutation"

    terminals.sessions[session_id] = [_entry(status="exited", exitCode=0, endReason="shell_exited")]
    service.refresh_terminal_sessions(session_id)
    assert len(published) == 2
    assert service.get_state(session_id)["terminal_sessions"][0]["status"] == "exited"


def test_refresh_skips_unmaterialized_session_without_creating_space(
    terminals: _FakeTerminals, published: list[TransportFrame], session_id: int
) -> None:
    terminals.sessions[session_id] = [_entry()]
    ConversationStateService().refresh_terminal_sessions(session_id)
    assert session_runtime_spaces.get(session_id) is None
    assert published == []
    assert terminals.calls == []


def test_refresh_skips_space_without_snapshot(
    terminals: _FakeTerminals, published: list[TransportFrame], session_id: int
) -> None:
    session_runtime_spaces.get_or_create(session_id)
    ConversationStateService().refresh_terminal_sessions(session_id)
    assert published == []
    assert terminals.calls == []


def test_refresh_after_session_deleted_is_silent(
    terminals: _FakeTerminals, published: list[TransportFrame], session_id: int
) -> None:
    service = ConversationStateService()
    service.get_state(session_id)
    session_runtime_spaces.mark_deleted(session_id)
    service.release_deleted_session(session_id)
    published.clear()
    terminals.sessions[session_id] = [_entry()]
    service.refresh_terminal_sessions(session_id)
    assert published == []
    # 从未物化的 Session 收到通知也不抛错。
    service.refresh_terminal_sessions(session_id + 1000)


def test_refresh_rejects_entries_violating_contract(
    terminals: _FakeTerminals, session_id: int
) -> None:
    """契约守卫在写入侧：违约条目在刷新写入时暴露，工作副本随之丢弃，之后的读取按 canonical 重建
    （重建时同样整份校验，违约照样暴露），不会把违约内容发出去。"""

    service = ConversationStateService()
    service.get_state(session_id)
    terminals.sessions[session_id] = [_entry(status="bogus")]
    with pytest.raises(ValueError):
        service.refresh_terminal_sessions(session_id)
    with pytest.raises(ValueError):
        service.get_state(session_id)
