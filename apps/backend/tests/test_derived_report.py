"""派生汇报：委派出的子 Task 每个 Run 落定都向派生方汇报。

真实 actor、ReAct、工具与 SQLite；只把模型流和命令进程换成可控替身。对标 Claude Code 实测：子 Agent
停下就汇报，还有后台工作时标为中间结果，之后被唤醒再汇报一次；汇报按 Task 关联，不绑定调用。
"""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from app.assistant_transport.service.transport_assistant_service import TransportAssistantService
from app.core.workflow import workflow
from app.core.workflow.branch import task_title
from app.core.workflow.message_queue.task_message import RunLaunch, TaskMessage, UserInputBody
from app.models.conversation_input import UserInputPresentation
from app.service import depends
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces as spaces
from app.tools.terminal_runtime.command.execution_result import ExecutionResult
from app.tools.tool_handler.terminal import execute_terminal
from app.tools.tool_handler.terminal.command_results import command_results
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
    """按请求里出现的输入决定回复；父子 Task 共用这一个替身，靠输入区分是谁在请求。"""

    def __init__(self, child_plan: str) -> None:
        self.child_plan = child_plan  # "answer" | "background" | "block"
        self.child_gate = asyncio.Event()
        self.parent_reports: list[str] = []
        self.child_started = asyncio.Event()

    async def stream(self, messages):
        inputs = [str(m.get("content")) for m in messages if m["role"] == "user"]
        tools_done = [m for m in messages if m["role"] == "tool"]
        if any('from="parent"' in text and ASSIGNMENT in text for text in inputs):
            frames = await self._child(inputs, tools_done)
        else:
            frames = self._parent(inputs, tools_done)
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

    def _parent(self, inputs, tools_done):
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
        self.parent_reports = reports
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
    parent = depends.get_workspace_service().create_task(workspace.id, "parent")
    state = SimpleNamespace(model=None, parent=parent, model_config=model_config)

    def resolve(**_):
        return SimpleNamespace(model=state.model, supports_thinking=False)

    _Command.gate = threading.Event()
    monkeypatch.setattr(execute_terminal, "LocalExecutionBackend", _Command)
    monkeypatch.setattr(workflow, "resolve_chat_model", resolve)
    monkeypatch.setattr(task_title, "should_launch", lambda *_: False)
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


def _send(task_id: int, model_config_id: str, text: str) -> None:
    spaces.enqueue(
        task_id,
        TaskMessage.of(
            UserInputBody(
                text=text,
                launch=RunLaunch(model_config_id=model_config_id),
                presentation=UserInputPresentation(text=text),
            )
        ),
    )


def _child_id(parent_id: int) -> int:
    [child] = depends.get_task_service().list_derived_tasks(parent_id)
    return child.id


def _runs(task_id: int):
    return depends.get_conversation_run_crud().list_by_task(task_id)


def _pending_kinds(task_id: int) -> list[str]:
    return [message.kind for message in depends.get_task_message_queue().pending_nowait(task_id)]


async def _settle(*task_ids: int) -> None:
    for task_id in task_ids:
        assert await spaces.get_or_create(task_id).actor.wait_quiescent(8)


async def _until(task_id: int, ready) -> None:
    """按 actor 的状态事件等条件成立（先取事件再判定，判定之后的变化一定会置位这次取到的事件）。"""
    actor = spaces.get_or_create(task_id).actor
    async with asyncio.timeout(8):
        while True:
            changed = actor.state_changed
            if await asyncio.to_thread(ready):
                return
            await changed.wait()


async def test_delegation_completes_at_once_and_the_child_reports_its_run(derived):
    """委派调用当场完成（没有后台占位）；子 Task 的 Run 落定后，结论以 Agent 消息汇报、开出父 Run。"""
    derived.model = _Model("answer")
    _send(derived.parent.id, derived.model_config.id, "start")
    await asyncio.wait_for(derived.model.child_started.wait(), 8)
    child_id = _child_id(derived.parent.id)
    await _settle(child_id, derived.parent.id)

    rows = depends.get_conversation_task_context_crud().get(derived.parent.id)
    [delegate] = [r for r in rows if r.message.get("tool_call_id") == "call-delegate"]
    assert delegate.metadata.status == "completed" and delegate.metadata.background is None
    assert [run.final_output for run in _runs(derived.parent.id)] == [
        "parent saw 0 reports",
        "parent saw 1 reports",
    ]
    [report] = derived.model.parent_reports
    assert f'source_task_id="{child_id}"' in report
    assert 'report="final" status="completed"' in report and "child result" in report
    child = depends.get_task_service().get_task(child_id)
    assert child.extra.deriving_call_id == "call-delegate"


async def test_background_work_makes_the_first_report_intermediate(derived):
    """子 Task 答复时后台命令还在跑：先报中间结果；命令结束唤醒它再落定，再报一次最终结果。"""
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
    """用户停止子 Task：只留一条不唤醒的通知；上游收回：什么也不交，父 Agent 已不要它的结果。"""
    derived.model = _Model("block")
    _send(derived.parent.id, derived.model_config.id, "start")
    await asyncio.wait_for(derived.model.child_started.wait(), 8)
    child_id = _child_id(derived.parent.id)
    if stop == "user":
        await TransportAssistantService().cancel(child_id)
    else:
        await spaces.get_or_create(child_id).actor.cancel("parent")
    derived.model.child_gate.set()
    await _until(child_id, lambda: _runs(child_id)[-1].status != "running")
    await _settle(child_id, derived.parent.id)

    assert _runs(child_id)[-1].end_reason == f"{stop}_cancelled"
    assert [run.final_output for run in _runs(derived.parent.id)] == ["parent saw 0 reports"]
    assert _pending_kinds(derived.parent.id) == (["system_notice"] if stop == "user" else [])


async def test_runs_the_user_opens_in_the_child_are_not_reported(derived):
    """用户在工作台直接和子 Task 对话开出的 Run 是用户与它之间的事，不向派生方汇报。"""
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
    """子 Task 还在跑时 ``wait_messages`` 挂起；子 Task 落定后无可等待。"""
    from app.core.tool.handler.wait_messages import WaitMessagesTool

    derived.model = _Model("block")
    _send(derived.parent.id, derived.model_config.id, "start")
    await asyncio.wait_for(derived.model.child_started.wait(), 8)
    context = SimpleNamespace(task_id=derived.parent.id, run_id=None, tool_call_id="call-wait")
    waiting = await asyncio.to_thread(WaitMessagesTool().execute, execution_context=context)
    assert waiting.suspend and "review" in (waiting.content or "")

    derived.model.child_gate.set()
    await _settle(_child_id(derived.parent.id), derived.parent.id)
    done = await asyncio.to_thread(WaitMessagesTool().execute, execution_context=context)
    assert done.status == "error" and not done.suspend


async def test_withdrawing_the_turn_recalls_its_child_and_drops_its_report(derived):
    """撤回发起委派的那一回合：这一回合派生的子 Task 以 ``parent`` 收回，不再汇报。"""
    derived.model = _Model("block")
    _send(derived.parent.id, derived.model_config.id, "start")
    await asyncio.wait_for(derived.model.child_started.wait(), 8)
    child_id = _child_id(derived.parent.id)
    parent_actor = spaces.get_or_create(derived.parent.id).actor
    await _until(
        derived.parent.id,
        lambda: not parent_actor.driving and _runs(derived.parent.id)[-1].status == "completed",
    )
    target = depends.get_conversation_task_context_service().latest_user_input(derived.parent.id)

    await TransportAssistantService().withdraw(derived.parent.id, target.message_id)
    derived.model.child_gate.set()
    await _settle(child_id, derived.parent.id)

    assert _runs(child_id)[-1].end_reason == "parent_cancelled"
    assert len(_runs(derived.parent.id)) == 1
    assert _pending_kinds(derived.parent.id) == []
