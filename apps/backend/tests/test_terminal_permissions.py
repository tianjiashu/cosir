"""执行批准、路径策略和沙箱外观察的行为回归；原生隔离另由 Rust 测试覆盖。"""

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.session_actor import SessionActor
from app.core.tool.schemas import ToolExecutionContext
from app.core.tool.terminal_approval.registry import terminal_approvals
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.models.session.session_command_errors import SessionCommandConflictError
from app.models.session.session_kind import SessionType
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces
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
    monkeypatch.setattr(module, "worker_sandbox", lambda: SimpleNamespace(supported=True))
    monkeypatch.setattr(module.LiveOutputChannel, "open", lambda _: None)
    from app.service import depends

    monkeypatch.setattr(
        depends,
        "get_session_service",
        lambda: SimpleNamespace(
            get_session=lambda _: SimpleNamespace(
                id=1, title="主会话", session_type=SessionType.USER, parent_session_id=None
            ),
            display_session_id=lambda _: 1,
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


def test_approval_runs_the_original_call_once_with_an_escalated_sandbox(terminal):
    workspace, data, scratch, context, calls = terminal
    with ThreadPoolExecutor() as pool:
        future = pool.submit(
            ExecuteTerminalTool().execute,
            command="original-command",
            escalate_sandbox=True,
            intent="Fetch dependency",
            execution_context=context,
        )
        request = pending()
        assert calls == []
        assert request["command"] == "original-command"
        # 审批卡的说明就是调用意图，与调用行同一个名字。
        assert request["intent"] == "Fetch dependency"
        with pytest.raises(SessionCommandConflictError):
            terminal_approvals.decide(999, request["id"], "approve")
        terminal_approvals.decide(1, request["id"], "approve")
        assert future.result(5).status == "success"
        with pytest.raises(SessionCommandConflictError):
            terminal_approvals.decide(1, request["id"], "approve")
    assert len(calls) == 1
    assert calls[0][0] == "original-command"
    assert calls[0][2].network is True
    assert "/" in calls[0][2].writable_roots
    assert not calls[0][2].can_read(data / "secret")


@pytest.mark.parametrize("decision", ["reject", "cancel"])
def test_reject_or_cancel_cannot_start_a_command(terminal, decision):
    *_, context, calls = terminal
    with ThreadPoolExecutor() as pool:
        future = pool.submit(
            ExecuteTerminalTool().execute,
            command="never-run",
            escalate_sandbox=True,
            intent="Access service",
            execution_context=context,
        )
        request = pending()
        if decision == "reject":
            terminal_approvals.decide(1, request["id"], "reject")
        else:
            terminal_approvals.cancel_originated(1, "stopped")
        assert future.result(5).status in {"error", "cancelled"}
    assert calls == []
    assert terminal_approvals.views(1) == []


def test_escalation_opens_network_and_writes_but_keeps_control_data_protected(terminal, tmp_path):
    """升级一次给足网络与工作区外写入；系统数据仍禁读写、控制目录仍只读，只有本会话 scratch 可写。"""
    workspace, data, scratch, _, _ = terminal
    policy = TerminalExecutionPolicy.for_command(
        mode="request_approval",
        workspace=workspace,
        writable_root=workspace,
        scratch=scratch,
        escalated=True,
    )
    # 强制执行在 Rust（见 sandbox_permissions.rs）；这里钉住交给它的事实。
    assert policy.network is True and "/" in policy.writable_roots
    assert str(scratch.resolve()) in policy.writable_exceptions
    assert str(data.resolve()) in policy.denied_read and not policy.can_read(data / "secret")
    readonly = set(policy.readonly_roots)
    assert {str(data.resolve()), str((workspace / ".cosir").resolve())} <= readonly
    # 运行后端的解释器也是控制程序：升级授写 ``/`` 时不能被改写。
    assert str(Path(sys.base_prefix).resolve()) in readonly


def test_default_sandbox_keeps_network_off_and_writes_inside_the_root(terminal, tmp_path):
    workspace, _, scratch, _, _ = terminal
    policy = TerminalExecutionPolicy.for_command(
        mode="request_approval",
        workspace=workspace,
        writable_root=workspace,
        scratch=scratch,
        escalated=False,
    )
    assert policy.network is False
    assert policy.writable_roots == (str(workspace.resolve()),)


def test_full_access_needs_no_approval(terminal):
    from dataclasses import replace

    *_, context, calls = terminal
    result = ExecuteTerminalTool().execute(
        command="native-command",
        escalate_sandbox=True,
        execution_context=replace(context, read_terminal_mode=lambda: "full_access"),
    )
    assert result.status == "success"
    assert len(calls) == 1 and calls[0][2].full_access
    assert terminal_approvals.views(1) == []


def test_cancel_racing_with_approval_reports_the_actual_winner(monkeypatch, tmp_path):
    from app.core.tool.terminal_approval.registry import TerminalApprovalRegistry

    registry = TerminalApprovalRegistry()
    request = registry.request(  # noqa: S604 - 登记权限请求，不调用 shell
        origin_session_id=1,
        display_session_id=1,
        run_id=1,
        tool_call_id="call",
        command="original",
        shell="sh",
        cwd=str(tmp_path),
        intent="test",
        origin_title="测试会话",
    )
    cancel = registry.cancel

    def approve_before_cancel(value, cause):
        registry.decide(1, request.request_id, "approve")
        return cancel(value, cause)

    monkeypatch.setattr(registry, "cancel", approve_before_cancel)
    # 批准赢得裁决时，调用方必须继续走既有的执行取消链，不能误报已停止。
    assert registry.cancel_call(1, "call") is False
    assert request.decision == "approve"


def test_effective_mode_is_the_strictest_along_ownership_and_applies_at_once(isolated_storage, tmp_path):
    """终端权限归会话：子 Agent 取所有权祖先中最严的那个，父会话降权立即对它生效，不需要它静止。"""
    from app.service import depends
    from app.storage.crud.session_crud import SessionCrud
    from app.storage.crud.workspace_crud import WorkspaceCrud

    workspace = WorkspaceCrud().create("test", str(tmp_path))
    sessions = SessionCrud()
    parent = sessions.create(workspace.id, "parent", terminal_permission_mode="full_access")
    child = sessions.create(
        workspace.id,
        "child",
        session_type=SessionType.DELEGATED,
        parent_session_id=parent.id,
        terminal_permission_mode="full_access",
    )
    service = depends.get_session_service()
    assert service.effective_terminal_mode(child.id) == "full_access"
    sessions.set_terminal_permission_mode(parent.id, "request_approval")
    assert service.effective_terminal_mode(child.id) == "request_approval"
    sessions.set_terminal_permission_mode(parent.id, "full_access")
    assert service.effective_terminal_mode(child.id) == "full_access"
    assert service.ownership_root_id(child.id) == parent.id


def test_session_send_refuses_a_broader_session_outside_its_tree(isolated_storage, tmp_path):
    """低权限会话不能借另一棵树里权限更宽的会话之手执行；同一棵树里（派生方与子 Agent）不受限。"""
    from app.core.tool.session_management.session_send import SessionSendTool
    from app.storage.crud.session_crud import SessionCrud
    from app.storage.crud.workspace_crud import WorkspaceCrud

    workspace = WorkspaceCrud().create("test", str(tmp_path))
    sessions = SessionCrud()
    restricted = sessions.create(workspace.id, "restricted", terminal_permission_mode="request_approval")
    broad = sessions.create(workspace.id, "broad", terminal_permission_mode="full_access")
    context = ToolExecutionContext(
        session_id=restricted.id,
        workspace_id=workspace.id,
        workspace_root=tmp_path,
        step_id="s",
        run_id=1,
        read_terminal_mode=lambda: "request_approval",
    )
    tool = SessionSendTool()
    assert tool._outranks_sender(sessions.get(broad.id), context) is True
    child = sessions.create(
        workspace.id,
        "child",
        session_type=SessionType.DELEGATED,
        parent_session_id=broad.id,
        terminal_permission_mode="full_access",
    )
    from dataclasses import replace

    child_context = replace(context, session_id=child.id)
    # 子 Agent 给自己的派生方发消息：同一棵树，不按权限拦。
    assert tool._outranks_sender(sessions.get(broad.id), child_context) is False


def test_terminal_notice_follows_the_effective_mode_and_is_not_repeated(monkeypatch):
    from app.core.workflow.node_helper.common import execution_scope
    from app.core.workflow.notices import request_notices as module
    from app.models.message_context.message_metadata.user_message_metadata import NoticeEntry
    from app.models.session.session_extra import SessionExtra
    from app.service import depends

    mode = {"value": "request_approval"}
    monkeypatch.setattr(
        depends,
        "get_session_service",
        lambda: SimpleNamespace(effective_terminal_mode=lambda _id: mode["value"]),
    )
    rc = SimpleNamespace(
        session=SimpleNamespace(id=3, extra=SessionExtra(writable_root="pkg")),
        workspace=SimpleNamespace(root_path="/workspace"),
    )
    latest: dict[str, object] = {"entry": None}
    context = SimpleNamespace(latest_notice=lambda _kind: latest["entry"])
    with execution_scope(rc, context):
        notice = module._terminal_permissions()
        assert notice is not None and "request_approval" in notice.content
        assert "/workspace/pkg" in notice.content and "exact command" in notice.content
        latest["entry"] = (1, NoticeEntry.model_validate(notice.entry().model_dump()))
        # 跨 Run 也不重发：基准是模型上次看到的权限与可写根。
        assert module._terminal_permissions() is None
        mode["value"] = "full_access"
        changed = module._terminal_permissions()
        assert changed is not None and "full_access" in changed.content


def test_inconclusive_sandbox_probes_are_retried(monkeypatch):
    """拉起失败或超时不缓存：一次冷启动超时不能让整个进程的请求批准模式都不可用。"""
    import subprocess

    from app.tools.terminal_runtime.permissions import policy as policy_module

    calls: list[str] = []

    def flaky(*_args, **_kwargs):
        calls.append("probe")
        if len(calls) == 1:
            raise subprocess.TimeoutExpired("worker", 15)
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(policy_module, "_probed", {})
    monkeypatch.setattr(policy_module.subprocess, "run", flaky)
    assert policy_module.probe_sandbox("/fake/worker").supported is False
    assert policy_module.probe_sandbox("/fake/worker").supported is True
    assert policy_module.probe_sandbox("/fake/worker").supported is True
    assert calls == ["probe", "probe"]
