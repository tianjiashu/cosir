"""派生汇报：委派出的子 Session 每个 Run 落定都向派生方汇报。

真实 actor、ReAct、工具与 SQLite；只把模型流和命令进程换成可控替身。对标 Claude Code 实测：子 Agent
停下就汇报，还有后台工作时标为中间结果，之后被唤醒再汇报一次；汇报按 Session 关联，不绑定调用。
"""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from app.assistant_transport.service.assistant_transport_service import AssistantTransportService
from app.core.tool.multi_agent.session_tool_scope import main_agent_tool_schemas
from app.core.workflow import workflow
from app.core.workflow.branch import session_title
from app.core.workflow.cancel.run_cancellation_registry import cancellation_registry
from app.core.workflow.message_queue.session_message import RunLaunch, SessionMessage, UserInputBody
from app.models.conversation_input import UserInputPresentation
from app.service import depends
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces as spaces
from app.tools.terminal_runtime.command.execution_result import ExecutionResult
from app.tools.tool_handler.terminal import execute_terminal
from app.tools.tool_handler.terminal.command_results import command_results
from tests.actor_settle_support import wait_session_settled
from tests.model_config_fixtures import create_model_config
from tests.sdk_fixtures import sdk_events

ASSIGNMENT = "child-work"


def _call(call_id: str, name: str, args: dict) -> dict:
    return {
        "index": 0,
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class _Model:
    """按请求里出现的输入决定回复；父子 Session 共用这一个替身，靠输入区分是谁在请求。"""

    def __init__(self, child_plan: str, *, parent_waits: bool = False) -> None:
        self.child_plan = child_plan  # "answer" | "background" | "block"
        # 父 Agent 委派后调用 ``wait_messages`` 挂起等子 Session，而不是直接答复。
        self.parent_waits = parent_waits
        self.child_gate = asyncio.Event()
        self.parent_reports: list[str] = []
        self.parent_notices: list[str] = []
        self.child_started = asyncio.Event()

    async def stream(self, messages):
        inputs = [str(m.get("content")) for m in messages if m["role"] == "user"]
        tools_done = [m for m in messages if m["role"] == "tool"]
        if any('from="parent"' in text and ASSIGNMENT in text for text in inputs):
            frames = await self._child(inputs, tools_done)
        else:
            frames = await self._parent(inputs, tools_done)
        for frame in frames:
            yield frame

    async def _child(self, inputs, tools_done):
        self.child_started.set()
        if self.child_plan == "block":
            await self.child_gate.wait()
        if self.child_plan == "background" and not tools_done:
            call = _call(
                "child-bg",
                "execute_terminal",
                {"intent": "build", "command": "build", "background": True},
            )
            return sdk_events([{"tool_calls": [call]}], finish_reason="tool_calls")
        finished = any("<background_result" in text for text in inputs)
        return sdk_events([{"content": "child done" if finished else "child result"}])

    async def _parent(self, inputs, tools_done):
        reports = [text for text in inputs if "report=" in text]
        if not tools_done and not reports:
            call = _call(
                "call-delegate",
                "delegate_task",
                {
                    "intent": "review",
                    "child_agent_id": "general-assistant",
                    "agent_name": "review",
                    "message": ASSIGNMENT,
                },
            )
            return sdk_events([{"tool_calls": [call]}], finish_reason="tool_calls")
        if self.parent_waits and len(tools_done) == 1 and not reports:
            # 子 Session 的 Run 开起来之后才有可等的东西。
            await self.child_started.wait()
            call = _call("call-wait", "wait_messages", {"intent": "wait for review"})
            return sdk_events([{"tool_calls": [call]}], finish_reason="tool_calls")
        self.parent_reports = reports
        self.parent_notices = [text for text in inputs if "<system_notice" in text]
        return sdk_events([{"content": f"parent saw {len(reports)} reports"}])

    async def close(self):
        pass


class _Command:
    """后台命令替身：放行前一直在跑。"""

    gate = threading.Event()

    def execute(self, command, cwd, timeout, output, **kwargs):
        assert self.gate.wait(10), "command was never released"
        output.feed(b"built\n")
        output.finish()
        return ExecutionResult(end="exited", exit_code=0)

    def stop(self):
        self.gate.set()


@pytest.fixture
async def derived(backend_env, tmp_path, monkeypatch):
    model_config = create_model_config(
        name="test",
        base_url="http://127.0.0.1",
        api_key="test",
        model_name="test",
        context_window_k=96,
        supports_thinking=True,
        supports_reasoning_effort=True,
        supports_image=False,
    )
    workspace = depends.get_workspace_service().create_workspace("ws", str(tmp_path))
    parent = depends.get_session_service().create_session(
        workspace.id, "parent", tool_schemas=main_agent_tool_schemas()
    )
    state = SimpleNamespace(model=None, parent=parent, model_config=model_config)

    def resolve(**_):
        return SimpleNamespace(model=state.model, supports_thinking=False)

    _Command.gate = threading.Event()
    monkeypatch.setattr(execute_terminal, "LocalExecutionBackend", _Command)
    monkeypatch.setattr(workflow, "resolve_chat_model", resolve)
    monkeypatch.setattr(session_title, "should_launch", lambda *_: False)
    spaces.bind_loop(asyncio.get_running_loop())
    command_results.open()
    try:
        yield state
    finally:
        _Command.gate.set()
        if state.model is not None:
            state.model.child_gate.set()
        await asyncio.to_thread(command_results.close)
        await spaces.aclose()
        command_results.open()


def _send(session_id: int, model_config_id: str, text: str) -> None:
    spaces.enqueue(
        session_id,
        SessionMessage.of(
            UserInputBody(
                text=text,
                launch=RunLaunch(model_config_id=model_config_id),
                presentation=UserInputPresentation(text=text),
            )
        ),
    )


def _child_id(parent_id: int) -> int:
    [child] = depends.get_session_service().list_derived_sessions(parent_id)
    return child.id


def _runs(session_id: int):
    return depends.get_run_crud().list_by_session(session_id)


def _pending_kinds(session_id: int) -> list[str]:
    return [message.kind for message in depends.get_session_message_queue().pending_nowait(session_id)]


async def _settle(*session_ids: int) -> None:
    for session_id in session_ids:
        spaces.get_or_create(session_id)
        await wait_session_settled(session_id)


async def _until(session_id: int, ready) -> None:
    """物化 Session 的 actor 后短间隔轮询，直到条件成立。"""
    spaces.get_or_create(session_id)
    async with asyncio.timeout(8):
        while True:
            if await asyncio.to_thread(ready):
                return
            await asyncio.sleep(0.02)


async def test_delegation_completes_at_once_and_the_child_reports_its_run(derived):
    """委派调用当场完成（没有后台占位）；子 Session 的 Run 落定后，结论以 Agent 消息汇报、开出父 Run。"""
    derived.model = _Model("answer")
    _send(derived.parent.id, derived.model_config.id, "start")
    await asyncio.wait_for(derived.model.child_started.wait(), 8)
    child_id = _child_id(derived.parent.id)
    await _settle(child_id, derived.parent.id)

    rows = depends.get_session_context_crud().get(derived.parent.id)
    [delegate] = [r for r in rows if r.message.get("tool_call_id") == "call-delegate"]
    assert delegate.metadata.status == "completed" and delegate.metadata.background is None
    assert [run.final_output for run in _runs(derived.parent.id)] == [
        "parent saw 0 reports",
        "parent saw 1 reports",
    ]
    [report] = derived.model.parent_reports
    assert f'source_session_id="{child_id}"' in report
    assert 'report="final" status="completed"' in report and "child result" in report
    child = depends.get_session_service().get_session(child_id)
    assert child.extra.deriving_call_id == "call-delegate"


async def test_background_work_makes_the_first_report_intermediate(derived):
    """子 Session 答复时后台命令还在跑：先报中间结果；命令结束唤醒它再落定，再报一次最终结果。"""
    derived.model = _Model("background")
    _send(derived.parent.id, derived.model_config.id, "start")
    await asyncio.wait_for(derived.model.child_started.wait(), 8)
    child_id = _child_id(derived.parent.id)

    await _until(
        derived.parent.id,
        lambda: len(_runs(derived.parent.id)) == 2
        and _runs(derived.parent.id)[-1].status == "completed",
    )
    [first] = derived.model.parent_reports
    assert 'report="intermediate" status="completed"' in first

    _Command.gate.set()
    await _settle(child_id, derived.parent.id)
    assert [run.final_output for run in _runs(child_id)] == ["child result", "child done"]
    second = derived.model.parent_reports[-1]
    assert 'report="final"' in second and "child done" in second


@pytest.mark.parametrize("stop", ["user", "parent"])
async def test_stopped_children_do_not_wake_the_deriver(derived, stop):
    """用户停止子 Session：只留一条不唤醒的通知；上游收回：什么也不交，父 Agent 已不要它的结果。"""
    derived.model = _Model("block")
    _send(derived.parent.id, derived.model_config.id, "start")
    await asyncio.wait_for(derived.model.child_started.wait(), 8)
    child_id = _child_id(derived.parent.id)
    if stop == "user":
        await AssistantTransportService().cancel(child_id)
    else:
        await spaces.get_or_create(child_id).actor.cancel("parent")
    derived.model.child_gate.set()
    await _until(child_id, lambda: _runs(child_id)[-1].status != "running")
    await _settle(child_id, derived.parent.id)

    assert _runs(child_id)[-1].end_reason == f"{stop}_cancelled"
    assert [run.final_output for run in _runs(derived.parent.id)] == ["parent saw 0 reports"]
    assert _pending_kinds(derived.parent.id) == (["system_notice"] if stop == "user" else [])


async def test_runs_the_user_opens_in_the_child_are_not_reported(derived):
    """用户在工作台直接和子 Session 对话开出的 Run 是用户与它之间的事，不向派生方汇报。"""
    derived.model = _Model("answer")
    _send(derived.parent.id, derived.model_config.id, "start")
    await asyncio.wait_for(derived.model.child_started.wait(), 8)
    child_id = _child_id(derived.parent.id)
    await _settle(child_id, derived.parent.id)

    _send(child_id, derived.model_config.id, f"from the user: {ASSIGNMENT}")
    await _settle(child_id, derived.parent.id)
    assert len(_runs(child_id)) == 2
    assert len(_runs(derived.parent.id)) == 2
    assert _pending_kinds(derived.parent.id) == []


async def test_wait_messages_waits_for_an_active_child(derived):
    """子 Session 还在跑时 ``wait_messages`` 挂起；子 Session 落定后无可等待。"""
    from app.core.tool.handler.wait_messages import WaitMessagesTool

    derived.model = _Model("block")
    _send(derived.parent.id, derived.model_config.id, "start")
    await asyncio.wait_for(derived.model.child_started.wait(), 8)
    context = SimpleNamespace(session_id=derived.parent.id, run_id=None, tool_call_id="call-wait")
    waiting = await asyncio.to_thread(WaitMessagesTool().execute, execution_context=context)
    assert waiting.suspend and "review" in (waiting.content or "")

    derived.model.child_gate.set()
    await _settle(_child_id(derived.parent.id), derived.parent.id)
    done = await asyncio.to_thread(WaitMessagesTool().execute, execution_context=context)
    assert done.status == "error" and not done.suspend


async def test_withdrawing_the_turn_recalls_its_child_and_drops_its_report(derived):
    """撤回发起委派的那一回合：这一回合派生的子 Session 以 ``parent`` 收回，不再汇报。"""
    derived.model = _Model("block")
    _send(derived.parent.id, derived.model_config.id, "start")
    await asyncio.wait_for(derived.model.child_started.wait(), 8)
    child_id = _child_id(derived.parent.id)
    parent_actor = spaces.get_or_create(derived.parent.id).actor
    await _until(
        derived.parent.id,
        lambda: not parent_actor.driving and _runs(derived.parent.id)[-1].status == "completed",
    )
    target = depends.get_session_context_service().latest_user_input(derived.parent.id)

    await AssistantTransportService().withdraw(derived.parent.id, target.message_id)
    derived.model.child_gate.set()
    await _settle(child_id, derived.parent.id)

    assert _runs(child_id)[-1].end_reason == "parent_cancelled"
    assert len(_runs(derived.parent.id)) == 1
    assert _pending_kinds(derived.parent.id) == []


async def _waiting_parent(derived) -> int:
    """父 Agent 委派后在 ``wait_messages`` 上挂起，子 Session 卡在模型调用里；返回子 Session id。"""
    derived.model = _Model("block", parent_waits=True)
    _send(derived.parent.id, derived.model_config.id, "start")
    await asyncio.wait_for(derived.model.child_started.wait(), 8)
    await _until(derived.parent.id, lambda: _runs(derived.parent.id)[-1].status == "suspended")
    return _child_id(derived.parent.id)


@pytest.mark.parametrize("ending", ["user_stop", "delete"])
async def test_a_deriver_waiting_on_a_stopped_child_wakes_up(derived, ending):
    """父 Agent 挂起等子 Session 时，子 Session 被用户停止或删除都不会再汇报：停止通知唤醒同一个父 Run，
    父 Agent 读到通知后自行收尾，不会一直挂着。"""
    child_id = await _waiting_parent(derived)
    if ending == "user_stop":
        await AssistantTransportService().cancel(child_id)
        derived.model.child_gate.set()
    else:
        # 删除先停下子 Session 并等它落定：停止信号打上之后再放行替身模型，驱动在下一个检查点看到
        # 停止（先放行的话子 Session 会抢先答完并汇报，测的就不是删除了）。
        deletion = asyncio.create_task(depends.get_session_service().delete_session_tree(child_id))
        async with asyncio.timeout(8):
            while not cancellation_registry.is_cancelled(child_id):  # noqa: ASYNC110 - 登记表没有可等的事件
                await asyncio.sleep(0.01)
        derived.model.child_gate.set()
        assert await deletion == frozenset({child_id})
    await _settle(derived.parent.id)

    [run] = _runs(derived.parent.id)
    assert run.status == "completed" and run.final_output == "parent saw 0 reports"
    [notice] = [text for text in derived.model.parent_notices if "which you derived" in text]
    assert f"session {child_id}" in notice
    assert ("deleted" if ending == "delete" else "stopped") in notice


async def test_a_deriver_waiting_across_a_restart_wakes_up(derived):
    """重启收敛了父 Agent 在等的子 Session：重启通知唤醒挂起的父 Run，而不是让它一直挂着。"""
    from app.service.recover_service import RecoverService

    child_id = await _waiting_parent(derived)
    # 模拟进程退出：执行被直接中止，子 Run 留在 running，父 Run 留在 suspended。
    await spaces.aclose()
    derived.model.child_gate.set()
    spaces.bind_loop(asyncio.get_running_loop())
    await asyncio.to_thread(RecoverService().recover_after_restart)
    await _settle(derived.parent.id)

    assert _runs(child_id)[-1].end_reason == "runtime_restarted"
    [run] = _runs(derived.parent.id)
    assert run.status == "completed"
    assert any("The backend restarted" in text for text in derived.model.parent_notices)
