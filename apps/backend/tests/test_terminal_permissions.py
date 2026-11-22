"""执行批准、路径策略和沙箱外观察的行为回归；原生隔离另由 Rust 测试覆盖。"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.session_actor import SessionActor
from app.core.tool.schemas import ToolExecutionContext
from app.core.tool.terminal_approval import terminal_approvals
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.models.session.session_command_errors import SessionCommandConflictError
from app.models.session.session_kind import SessionType
from app.models.terminal_permissions import TerminalPermissions
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces
from app.tools.shell.file_access.command_effects import TerminalRead
from app.tools.shell.file_access.read_evidence import TerminalReadEvidence
from app.tools.terminal_runtime.command.execution_result import ExecutionResult
from app.tools.terminal_runtime.permissions import policy as policy_module
from app.tools.terminal_runtime.permissions.policy import TerminalExecutionPolicy
from app.tools.tool_handler.terminal import execute_terminal as module
from app.tools.tool_handler.terminal.execute_terminal import ExecuteTerminalTool


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".cosir").mkdir()
    data = tmp_path / "data"
    scratch = data / "scratch" / "1"
    scratch.mkdir(parents=True)
    monkeypatch.setattr(policy_module, "system_cosir_dir", lambda: data)
    monkeypatch.setattr(policy_module, "system_scratch_dir", lambda: data / "scratch")
    monkeypatch.setattr(policy_module, "system_cosir_skills_dir", lambda: data / "skills")
    monkeypatch.setattr(module, "probe_sandbox", lambda _: SimpleNamespace(supported=True))
    monkeypatch.setattr(module.LiveOutputChannel, "open", lambda _: None)
    from app.service import depends

    monkeypatch.setattr(
        depends,
        "get_session_service",
        lambda: SimpleNamespace(
            get_session=lambda _: SimpleNamespace(
                id=1, session_type=SessionType.USER, parent_session_id=None
            )
        ),
    )
    space = SimpleNamespace(session_id=1, mailbox=RunMailbox(), operation=threading.RLock)
    space.actor = SessionActor(space)
    monkeypatch.setattr(session_runtime_spaces, "get_or_create", lambda _: space)
    calls = []

    class Backend:
        def execute(self, command, cwd, timeout, output, **kwargs):
            calls.append((command, cwd, kwargs["policy"]))
            return ExecutionResult(end="exited", exit_code=0)

        def stop(self):
            pass

    monkeypatch.setattr(module, "LocalExecutionBackend", Backend)
    context = ToolExecutionContext(
        session_id=1,
        workspace_id=1,
        workspace_root=workspace,
        step_id="step-1",
        run_id=1,
        tool_call_id="original-call",
    )
    terminal_approvals.close()
    terminal_approvals.set_listener(None)
    yield workspace, data, scratch, context, calls
    terminal_approvals.close()


def pending():
    deadline = time.monotonic() + 3
    while not terminal_approvals.views(1):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    return terminal_approvals.views(1)[0]


def test_approval_runs_the_original_call_once_with_separate_network_permission(terminal):
    workspace, data, scratch, context, calls = terminal
    with ThreadPoolExecutor() as pool:
        future = pool.submit(
            ExecuteTerminalTool().execute,
            command="original-command",
            permissions={"network": True},
            justification="Fetch dependency",
            execution_context=context,
        )
        request = pending()
        assert calls == []
        assert request["command"] == "original-command"
        with pytest.raises(SessionCommandConflictError):
            terminal_approvals.decide(999, request["id"], "approve")
        terminal_approvals.decide(1, request["id"], "approve")
        assert future.result(5).status == "success"
        with pytest.raises(SessionCommandConflictError):
            terminal_approvals.decide(1, request["id"], "approve")
    assert len(calls) == 1
    assert calls[0][0] == "original-command"
    assert calls[0][2].network is True
    assert not calls[0][2].can_write(workspace.parent / "outside")
    assert not calls[0][2].can_read(data / "secret")


@pytest.mark.parametrize("decision", ["reject", "cancel"])
def test_reject_or_cancel_cannot_start_a_command(terminal, decision):
    *_, context, calls = terminal
    with ThreadPoolExecutor() as pool:
        future = pool.submit(
            ExecuteTerminalTool().execute,
            command="never-run",
            permissions={"network": True},
            justification="Access service",
            execution_context=context,
        )
        request = pending()
        if decision == "reject":
            terminal_approvals.decide(1, request["id"], "reject")
        else:
            terminal_approvals.cancel_session(1)
        assert future.result(5).status in {"error", "cancelled"}
    assert calls == []
    assert terminal_approvals.views(1) == []


def test_scoped_write_grant_retains_control_data_protection(terminal, tmp_path):
    workspace, data, scratch, _, _ = terminal
    extra = tmp_path / "extra"
    extra.mkdir()
    policy = TerminalExecutionPolicy.for_command(
        mode="request_approval",
        workspace=workspace,
        writable_root=workspace,
        scratch=scratch,
        permissions=TerminalPermissions(write_roots=[str(tmp_path)]),
    )
    assert policy.can_write(extra / "new-file")
    assert policy.can_write(scratch / "new-file")
    assert not policy.can_write(data / "secret")
    assert not policy.can_write(workspace / ".cosir" / "mcp.json")
    assert policy.network is False


def test_python_read_evidence_never_opens_a_forbidden_file(terminal, monkeypatch):
    workspace, data, scratch, _, _ = terminal
    secret = data / "secret"
    secret.write_text("forged output")
    policy = TerminalExecutionPolicy.for_command(
        mode="request_approval",
        workspace=workspace,
        writable_root=workspace,
        scratch=scratch,
        permissions=TerminalPermissions(),
    )
    original = Path.open

    def guarded(path, *args, **kwargs):
        assert path != secret, "Python observation read a sandbox-denied file"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    assert TerminalReadEvidence.sample(TerminalRead(secret, "whole"), policy) is None


def test_full_access_needs_no_approval(terminal):
    from dataclasses import replace

    *_, context, calls = terminal
    result = ExecuteTerminalTool().execute(
        command="native-command",
        permissions={"network": True},
        execution_context=replace(context, terminal_permission_mode="full_access"),
    )
    assert result.status == "success"
    assert len(calls) == 1 and calls[0][2].full_access
    assert terminal_approvals.views(1) == []


def test_cancel_racing_with_approval_reports_the_actual_winner(monkeypatch, tmp_path):
    from app.core.tool.terminal_approval import TerminalApprovalRegistry

    registry = TerminalApprovalRegistry()
    request = registry.request(  # noqa: S604 - 登记权限请求，不调用 shell
        origin_session_id=1,
        display_session_id=1,
        run_id=1,
        tool_call_id="call",
        command="original",
        shell="sh",
        cwd=str(tmp_path),
        network=True,
        write_roots=(),
        justification="test",
    )
    cancel = registry.cancel

    def approve_before_cancel(value):
        registry.decide(1, request.request_id, "approve")
        return cancel(value)

    monkeypatch.setattr(registry, "cancel", approve_before_cancel)
    # 批准赢得裁决时，调用方必须继续走既有的执行取消链，不能误报已停止。
    assert registry.cancel_call(1, "call") is False
    assert request.decision == "approve"


@pytest.mark.parametrize("queued_downgrade", [False, True])
def test_old_child_background_result_cannot_restore_full_access(
    isolated_storage, tmp_path, monkeypatch, queued_downgrade
):
    from dataclasses import replace

    from app.core.events.event_sink import set_event_sink
    from app.core.workflow import run_ledger as ledger_module
    from app.core.workflow.dispatch_policy import Action, StartRun
    from app.core.workflow.message_queue.pending_messages import PendingMessages
    from app.core.workflow.message_queue.session_message import (
        BackgroundResultBody,
        RunLaunch,
        SessionMessage,
        UserInputBody,
    )
    from app.core.workflow.message_queue.session_message_queue import SessionMessageQueue
    from app.core.workflow.run_ledger import RunLedger
    from app.models.message_context.message_metadata.user_message_metadata import (
        UserInputPresentation,
    )
    from app.storage.crud.session_crud import SessionCrud
    from app.storage.crud.workspace_crud import WorkspaceCrud

    set_event_sink(lambda _event: None)
    monkeypatch.setattr(
        ledger_module,
        "get_model_config_service",
        lambda: SimpleNamespace(
            get_config=lambda _: SimpleNamespace(model=SimpleNamespace(context_window_k=64))
        ),
    )
    workspace = WorkspaceCrud().create("test", str(tmp_path))
    sessions = SessionCrud()
    parent = sessions.create(workspace.id, "parent")
    child = sessions.create(
        workspace.id, "child", session_type=SessionType.DELEGATED, parent_session_id=parent.id
    )
    ledger = RunLedger()
    full = RunLaunch(model_config_id="test/model", terminal_permission_mode="full_access")
    restricted = full.model_copy(update={"terminal_permission_mode": "request_approval"})
    ledger.create_run(parent.id, full)
    old_child = replace(ledger.create_run(child.id, full), status="completed")
    if queued_downgrade:
        # parent 输入已在树闸门内入队，child actor 比 parent actor 更早获得执行机会。
        SessionMessageQueue().put(
            parent.id,
            SessionMessage.of(
                UserInputBody(
                    text="lower permissions",
                    launch=restricted,
                    presentation=UserInputPresentation(text="lower permissions"),
                )
            ),
        )
    else:
        ledger.create_run(parent.id, restricted)
    completion = SessionMessage.of(
        BackgroundResultBody(
            tool_call_id="old-full-command",
            tool_name="execute_terminal",
            tool_result="done",
            ended_at="2026-11-22T00:00:00+00:00",
        )
    )
    action = Action.decide("idle", old_child, PendingMessages.of([completion]), [completion])
    assert isinstance(action, StartRun) and action.launch.terminal_permission_mode == "full_access"
    new_child = ledger.create_run(child.id, action.launch)
    assert new_child.terminal_permission_mode == "request_approval"
    assert ledger.get_run(new_child.id).terminal_permission_mode == "request_approval"


def test_terminal_notice_uses_frozen_run_and_is_announced_once():
    from app.core.workflow.notices.drive_notices import _terminal_permissions
    from app.models.message_context.message_metadata.user_message_metadata import NoticeEntry
    from app.models.session.session_extra import SessionExtra

    rc = SimpleNamespace(
        run=SimpleNamespace(id=7, terminal_permission_mode="request_approval"),
        session=SimpleNamespace(extra=SessionExtra(writable_root="pkg")),
        workspace=SimpleNamespace(root_path="/workspace"),
    )
    notice = _terminal_permissions(rc, SimpleNamespace(latest_notice=lambda _: None))
    assert notice is not None and "request_approval" in notice.content
    assert "/workspace/pkg" in notice.content and "exact command" in notice.content
    already = SimpleNamespace(
        latest_notice=lambda _: (
            7,
            NoticeEntry(notice_kind="terminal_permissions", content=notice.content),
        )
    )
    assert _terminal_permissions(rc, already) is None
