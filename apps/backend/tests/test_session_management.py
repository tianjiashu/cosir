"""会话管理工具（``core/tool/session_management``）：进度投影、尾部读取、派生树寻址与列表、``session_stop`` 的判定。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

# 与生产装配顺序一致，避免隔离收集时的旧导入环。
import app.tools  # noqa: F401
from app.core.context.context_store import ContextStore
from app.core.tool.schemas import ToolExecutionContext
from app.core.tool.session_management import addressing, session_list, session_stop
from app.core.tool.session_management.progress import SessionProgress
from app.core.tool.session_management.session_list import SessionListTool
from app.core.tool.session_management.session_stop import SessionStopTool
from app.models.conversation_input import MessageSource, UserInputPresentation
from app.models.message_metadata import BackgroundWork, ToolMessageMetadata, UserMessageMetadata
from app.models.run_record import RunRecord
from app.models.session_context import SessionContextRecord
from app.models.session_extra import SessionExtra, SessionFork, SessionForkLaunch
from app.models.session_kind import SessionType
from app.models.session_record import SessionRecord
from app.service.session.session_service import SessionService
from app.storage.crud.run_crud import RunCrud
from app.storage.crud.session_context_crud import SessionContextCrud
from app.storage.crud.session_crud import SessionCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.store_engines import main_db_factory

_START = datetime(2026, 11, 21, 8, 0, tzinfo=UTC)


def _session(session_id: int = 7, deriver: int = 1) -> SessionRecord:
    return SessionRecord(
        id=session_id, workspace_id=1, title="审查后端", created_at=_START, updated_at=_START,
        session_type=SessionType.DELEGATED, parent_session_id=deriver, current_run_id=3,
    )


def _run(status: str, **fields: object) -> RunRecord:
    return RunRecord(id=3, session_id=7, status=status, created_at=_START, updated_at=_START, **fields)


def _row(sequence: int, message: dict, metadata: object = None, *, include: bool = True) -> SessionContextRecord:
    extra = {} if metadata is None else {"metadata": metadata}
    return SessionContextRecord(
        session_id=7, run_id=3, message=message, include_in_context=include, sequence=sequence, turn=1, **extra
    )


def _assistant(text: str = "", *calls: tuple[str, str, dict]) -> dict:
    message: dict = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for call_id, name, args in calls
        ]
    return message


def _tool(call_id: str, status: str, background: BackgroundWork | None = None) -> tuple[dict, ToolMessageMetadata]:
    return (
        {"role": "tool", "tool_call_id": call_id, "content": "ok"},
        ToolMessageMetadata(status=status, background=background),
    )


def _project(run: RunRecord | None, rows: list[SessionContextRecord], **overrides: object) -> SessionProgress:
    facts: dict = {"queued": 0, "waiting_on": [], "steps": 8, "now": _START + timedelta(minutes=2, seconds=5)}
    facts.update(overrides)
    return SessionProgress.from_facts(_session(), run, rows, **facts)


def _user_input(sequence: int, text: str) -> SessionContextRecord:
    return _row(
        sequence, {"role": "user", "content": text},
        UserMessageMetadata(kind="user_input", presentation=UserInputPresentation(text=text)),
    )


def test_running_tool_call_is_what_it_is_doing_now() -> None:
    rows = [
        _user_input(1, "审查 api 层"),
        _row(2, _assistant("先读路由", ("c1", "read_file", {"path": "app/api/a.py"}))),
        _row(3, *_tool("c1", "completed")),
        _row(4, _assistant("", ("c2", "execute_terminal", {"command": "pytest -q", "intent": "跑后端测试"}))),
    ]
    progress = _project(_run("running"), rows)

    assert progress.now == "running tools: execute_terminal"
    assert progress.elapsed == "for 2m5s"
    assert progress.steps == (
        'user said "审查 api 层"',
        'said "先读路由"; read_file({"path":"app/api/a.py"}) → ok',
        "execute_terminal(跑后端测试) → running",
    )
    assert progress.summary_line() == (
        "#7 审查后端 (delegated child agent) — running for 2m5s — running tools: execute_terminal"
    )


def test_streaming_reply_and_closed_batch() -> None:
    streaming = [_row(1, _assistant("正在写结论"), include=False)]
    assert _project(_run("running"), streaming).now == "writing a reply"
    assert _project(_run("running"), streaming).steps == ('writing a reply…; "正在写结论"',)

    closed = [_row(1, _assistant("", ("c1", "find_files", {"pattern": "*.py"}))), _row(2, *_tool("c1", "failed"))]
    progress = _project(_run("running"), closed)
    assert progress.now == "waiting for the model"
    assert progress.steps == ('find_files({"pattern":"*.py"}) → failed',)


def test_interrupted_output_of_a_stopped_run_is_not_live() -> None:
    rows = [_row(1, _assistant("写了一半", ("c1", "read_file", {"path": "x"})), include=False)]
    progress = _project(_run("cancelled", end_reason="parent_cancelled"), rows)

    assert progress.now == "idle (run ended)"
    assert progress.steps == ('interrupted output; "写了一半"; read_file({"path":"x"}) → no result',)
    assert progress.end_reason == "stopped from upstream (you or your deriver)"
    assert progress.elapsed == "took 0s"


def test_suspended_says_what_it_waits_for() -> None:
    work = BackgroundWork(run_id=3, label="dev server", started_at=_START.isoformat())
    rows = [
        _row(1, _assistant("", ("c1", "execute_terminal", {"intent": "dev server"}))),
        _row(2, *_tool("c1", "pending", work)),
    ]
    progress = _project(_run("suspended"), rows, waiting_on=["background command: dev server"], queued=2)

    assert progress.now == "paused, waiting for background command: dev server"
    assert progress.steps == ("execute_terminal(dev server) → running in background",)
    assert "queued: 2 message(s) not yet taken in" in progress.render()
    assert progress.summary_line().endswith("; 2 queued")
    assert _project(_run("suspended"), []).now == "paused, waiting for messages"


def test_finished_run_renders_final_output_and_trims_steps() -> None:
    rows = [_user_input(index, f"第 {index} 条") for index in range(1, 6)]
    rows.append(
        _row(
            6, {"role": "user", "content": "补充一点"},
            UserMessageMetadata(
                kind="agent_message", presentation=UserInputPresentation(text="补充一点"),
                source=MessageSource(session_id=1, name="主会话"),
            ),
        )
    )
    progress = _project(_run("completed", final_output="没有发现问题。"), rows, steps=2)

    assert progress.steps == ('user said "第 5 条"', 'message from 主会话: "补充一点"')
    rendered = progress.render()
    assert rendered.endswith("final output:\n没有发现问题。")
    assert "status: completed (took 0s)" in rendered
    assert _project(_run("completed"), rows, steps=0).steps == ()


def test_render_says_when_older_steps_are_hidden() -> None:
    rows = [_user_input(index, f"第 {index} 条") for index in range(1, 4)]

    assert "recent steps (all steps; oldest first):" in _project(_run("running"), rows).render()
    assert "recent steps (last 2 steps, older ones not shown; oldest first):" in _project(
        _run("running"), rows, steps=2
    ).render()
    # 窗口读满时更早的行没读出来，即使显示的步数没超过 steps 也要说明。
    assert "older ones not shown" in _project(_run("running"), rows, window_full=True).render()


def test_marks_a_run_the_user_opened() -> None:
    progress = _project(_run("running"), [], user_driven=True)

    assert progress.summary_line().endswith("; the user is talking to it directly")
    assert "user: the user is working in it directly; this run is theirs" in progress.render()
    assert "user:" not in _project(_run("running"), []).render()


def test_read_points_to_wait_messages_only_while_a_report_is_still_coming() -> None:
    hint = "next: it reports back to you when this run ends"
    assert hint in _project(_run("running"), []).render()
    assert hint in _project(_run("suspended"), [], waiting_on=["derived session: 子任务"]).render()
    assert hint in _project(None, []).render()
    assert hint not in _project(_run("completed"), []).render()
    # 用户的 Run 不汇报：不能叫派生方去等，改说别等。
    user_run = _project(_run("running"), [], user_driven=True).render()
    assert hint not in user_run
    assert "so do not wait for it" in user_run


def test_run_opened_by_user_skips_notices_and_ignores_agent_input(isolated_storage, tmp_path) -> None:
    workspace = WorkspaceCrud().create("opened", str(tmp_path))
    session = SessionCrud().create(workspace.id, "opened")
    by_user = RunCrud().create(session.id, status="running")
    by_agent = RunCrud().create(session.id, status="running")
    store = ContextStore()
    notice = UserMessageMetadata(kind="notice")
    store.append(session.id, by_user.id, {"role": "user", "content": "handover"}, 1, metadata=notice, turn=1)
    store.append(
        session.id, by_user.id, {"role": "user", "content": "帮我看看"}, 2,
        metadata=UserMessageMetadata(kind="user_input", presentation=UserInputPresentation(text="帮我看看")), turn=1,
    )
    store.append(
        session.id, by_agent.id, {"role": "user", "content": "继续"}, 3,
        metadata=UserMessageMetadata(
            kind="agent_message", presentation=UserInputPresentation(text="继续"),
            source=MessageSource(session_id=1, name="主会话"),
        ),
        turn=2,
    )

    assert store.run_opened_by_user(session.id, by_user.id)
    assert not store.run_opened_by_user(session.id, by_agent.id)


def test_not_started_session() -> None:
    progress = _project(None, [], queued=1)

    assert progress.status is None
    assert progress.summary_line() == (
        "#7 审查后端 (delegated child agent) — not started — waiting for its first run to start; 1 queued"
    )
    assert "status: not started yet" in progress.render()


def test_recent_run_rows_skip_inherited_and_withdrawn_rows(isolated_storage, tmp_path) -> None:
    workspace = WorkspaceCrud().create("progress", str(tmp_path))
    session = SessionCrud().create(workspace.id, "progress")
    run = RunCrud().create(session.id, status="running")
    store = ContextStore()
    store.append(session.id, None, {"role": "assistant", "content": "inherited"}, 1, turn=1)
    store.append(session.id, run.id, {"role": "assistant", "content": "withdrawn"}, 2, turn=2)
    for sequence in range(3, 7):
        store.append(session.id, run.id, {"role": "assistant", "content": f"a{sequence}"}, sequence, turn=3)
    crud = SessionContextCrud()
    withdrawn = next(row for row in crud.get(session.id) if row.sequence == 2)
    with main_db_factory().begin() as db:
        crud.exclude_rows(session.id, frozenset({withdrawn.id}), db)

    assert [row.sequence for row in crud.list_recent_run_rows(session.id, 3)] == [4, 5, 6]
    assert [row.sequence for row in crud.list_recent_run_rows(session.id, 10)] == [3, 4, 5, 6]


class _Actor:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def request_cancel(self, reason: str) -> None:
        self.reasons.append(reason)


class _SessionCrud:
    """派生树：1 是调用方，2 是它委派的子 Agent，7 是 2 委派的孙辈，8 是 2 分叉出的孙辈；5、9 与 1 无关。"""

    def __init__(self) -> None:
        self.sessions = {
            1: _user_session(1),
            2: _session(2, deriver=1),
            7: _session(7, deriver=2),
            8: _fork(8, source=2),
            5: _user_session(5),
            9: _session(9, deriver=5),
        }

    def get(self, session_id: int) -> SessionRecord:
        return self.sessions[session_id]

    def list_by_parent_session(self, session_id: int) -> list[SessionRecord]:
        return [
            s
            for s in self.sessions.values()
            if s.session_type == SessionType.DELEGATED and s.parent_session_id == session_id
        ]

    def list_by_workspace_and_type(self, _workspace_id: int, session_type: SessionType) -> list[SessionRecord]:
        return [s for s in self.sessions.values() if s.session_type == session_type]


def _user_session(session_id: int) -> SessionRecord:
    return SessionRecord(id=session_id, workspace_id=1, title=f"会话{session_id}", created_at=_START, updated_at=_START)


def _fork(session_id: int, *, source: int) -> SessionRecord:
    return SessionRecord(
        id=session_id, workspace_id=1, title="分叉", created_at=_START, updated_at=_START,
        session_type=SessionType.AGENT_FORK,
        extra=SessionExtra(fork=SessionFork(
            source_session_id=source, source_title="子", tree_parent_session_id=None,
            initial_launch=SessionForkLaunch(agent_id=None, model_config_id=None, reasoning_effort=None),
        )),
    )


def _tree_service(run: RunRecord | None = None) -> SessionService:
    service = SessionService.__new__(SessionService)
    service._session = _SessionCrud()  # type: ignore[assignment]
    service.require_current_run = lambda _id: run  # type: ignore[method-assign]
    return service


def _context(caller: int = 1) -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id=caller, run_id=9, tool_call_id="call", workspace_id=1, workspace_root="/w", step_id=1
    )


def _stop(monkeypatch, target_id: int, run: RunRecord | None, *, caller: int = 1):
    service = _tree_service(run)
    monkeypatch.setattr(addressing, "get_session_service", lambda: service)
    monkeypatch.setattr(session_stop, "get_session_service", lambda: service)
    actor = _Actor()
    spaces = SimpleNamespace(get_or_create=lambda _id: SimpleNamespace(actor=actor))
    monkeypatch.setattr(session_stop, "session_runtime_spaces", spaces)
    return SessionStopTool().execute(target_id, execution_context=_context(caller)), actor


def test_derivation_tree_is_preorder_and_follows_forks() -> None:
    service = _tree_service()

    assert [s.id for s in service.list_derivation_tree(1)] == [2, 7, 8]
    assert service.derives_from(service.get_session(8), 1)
    assert not service.derives_from(service.get_session(9), 1)


def test_stop_recalls_a_direct_derived_session(monkeypatch) -> None:
    observation, actor = _stop(monkeypatch, 2, _run("suspended"))

    assert observation.status == "success"
    assert actor.reasons == ["parent"]
    assert "stop requested for session 2 (was suspended)" in str(observation.content)
    assert observation.display_data == {
        "kind": "agent-session-result", "operation": "stop", "session_id": 2, "agent_name": "审查后端",
    }


def test_stop_says_idle_instead_of_pretending(monkeypatch) -> None:
    for run in (None, _run("completed")):
        observation, actor = _stop(monkeypatch, 2, run)
        assert observation.status == "success"
        assert actor.reasons == []
        assert observation.content == "session 2 is idle; nothing to stop."


def test_stop_never_skips_a_level(monkeypatch) -> None:
    """越级停孙辈会让等着它的子 Agent 永远挂着：拒绝并指回它的派生方。"""
    observation, actor = _stop(monkeypatch, 7, _run("running"))
    assert observation.status == "error"
    assert actor.reasons == []
    assert "was derived by session 2, not by you" in str(observation.content)

    for target, hint in ((9, "not in your derivation tree"), (1, "your own session")):
        observation, actor = _stop(monkeypatch, target, _run("running"))
        assert observation.status == "error" and hint in str(observation.content)
        assert actor.reasons == []


def test_read_reaches_the_whole_tree_but_not_beyond(monkeypatch) -> None:
    service = _tree_service()
    monkeypatch.setattr(addressing, "get_session_service", lambda: service)

    for target in (2, 7, 8):
        assert isinstance(addressing.find_readable_session("session_read", target, _context()), SessionRecord)
    refused = addressing.find_readable_session("session_read", 9, _context())
    assert refused.status == "error"


def test_list_indents_the_tree_and_marks_what_you_may_do(monkeypatch) -> None:
    service = _tree_service()
    monkeypatch.setattr(session_list, "get_session_service", lambda: service)
    monkeypatch.setattr(
        SessionProgress, "read",
        classmethod(lambda cls, session, *, steps: _project(_run("running"), [])),
    )
    # 投影用同一个假 Session；这里只看树形、派生方与标注。
    monkeypatch.setattr(
        SessionProgress, "summary_line", lambda self, *, derived_by=None: f"line(derived_by={derived_by})"
    )

    lines = SessionListTool().execute(execution_context=_context()).content.splitlines()

    assert lines[0].startswith("Your derivation tree")
    assert lines[1:] == [
        "line(derived_by=None) [read, stop]",
        "  line(derived_by=2) [read]",
        "  line(derived_by=2) [read]",
    ]
    assert SessionListTool().execute(execution_context=_context(5)).content.splitlines()[1:] == [
        "line(derived_by=None) [read, stop]",
    ]
