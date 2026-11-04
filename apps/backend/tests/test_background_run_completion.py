"""真实 actor、ReAct、工具与 SQLite 回投链路；仅模型流和命令进程用可控替身。"""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from app.assistant_transport.service.transport_assistant_service import TransportAssistantService
from app.core.hook import hook_registry
from app.core.tool.background.backgraound_registry import background_registry
from app.core.workflow import workflow
from app.core.workflow.branch import task_title
from app.core.workflow.message_queue.task_message import RunLaunch, TaskMessage, UserInputBody
from app.models.conversation_input import UserInputPresentation
from app.service import depends
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces as spaces
from app.tools.tool_handler import execute_terminal
from app.tools.tool_handler.terminal import ExecutionResult
from tests.sdk_fixtures import sdk_events


@pytest.mark.parametrize(
    "last", ["command", "delegate", "fork", "cancel", "restart", "queued_restart", "delegate_restart"]
)
async def test_results_resume_one_run_in_batches(backend_env, tmp_path, monkeypatch, last):
    model = depends.get_model_config_service().create_config(
        config_name="test",
        base_url="http://127.0.0.1",
        api_key="test",
        model_name="test",
        context_window_k=96,
        supports_thinking=True,
        supports_reasoning_effort=True,
        supports_image=False,
    )
    workspace = depends.get_workspace_service().create_workspace("ws", str(tmp_path))
    task = depends.get_workspace_service().create_task(workspace.id, "parent")
    gates = {name: threading.Event() for name in ("A", "B")}
    child_gate = asyncio.Event()
    saw_first = asyncio.Event()
    parent_inputs = []

    class Command:
        def __init__(self):
            self.stopped = threading.Event()
            self.command = None

        def execute(self, command, cwd, timeout, **kwargs):
            self.command = command
            if not self.stopped.is_set():
                assert gates[command].wait(10), "command was never released"
            return ExecutionResult(
                output=command, exit_code=0, timed_out=False, cancelled=self.stopped.is_set()
            )

        def stop(self):
            self.stopped.set()
            if self.command:
                gates[self.command].set()

    class Model:
        async def stream(self, messages):
            inputs = [str(m.get("content")) for m in messages if m["role"] == "user"]
            if any("<agent_message " in text and "child-report" in text for text in inputs):
                await child_gate.wait()
                frames = sdk_events([{"content": "child finished"}])
            else:
                results = [text for text in inputs if "<background_result time=" in text]
                parent_inputs.append(results)
                if results:
                    saw_first.set()
                if len(parent_inputs) == 1:

                    def cmd(label):
                        return "execute_terminal", {
                            "description": label,
                            "command": label,
                            "background": True,
                        }

                    second = {
                        "delegate": (
                            "delegate_task",
                            {
                                "child_agent_id": "general-assistant",
                                "agent_name": "review",
                                "message": "child-report",
                            },
                        ),
                        "fork": ("fork_task", {"title": "review", "message": "child-report"}),
                    }.get("delegate" if last == "delegate_restart" else last, cmd("B"))
                    calls = [
                        {
                            "index": i,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                        for i, (call_id, (name, args)) in enumerate(
                            [("bg-A", cmd("A")), ("bg-B", second)]
                        )
                    ]
                    frames = sdk_events([{"tool_calls": calls}], finish_reason="tool_calls")
                else:
                    frames = sdk_events([{"content": f"answer with {len(results)} results"}])
            for frame in frames:
                yield frame

        async def close(self):
            pass

    async def parked(actor):
        async with asyncio.timeout(8):
            while True:
                changed = actor.state_changed
                current = await asyncio.to_thread(
                    depends.get_task_service().require_current_run, task.id
                )
                if current is not None and current.status != "running" and not actor.driving:
                    return current
                await changed.wait()

    monkeypatch.setattr(hook_registry, "_registry", hook_registry.HookRegistry())
    monkeypatch.setattr(execute_terminal, "LocalExecutionBackend", Command)
    monkeypatch.setattr(
        workflow,
        "resolve_chat_model",
        lambda **_: SimpleNamespace(model=Model(), supports_thinking=False),
    )
    monkeypatch.setattr(task_title, "should_launch", lambda *_: False)
    spaces.bind_loop(asyncio.get_running_loop())
    background_registry.open()
    try:
        spaces.enqueue(
            task.id,
            TaskMessage.of(
                UserInputBody(
                    input_text="start",
                    launch=RunLaunch(model_config_id=model.id),
                    presentation=UserInputPresentation(display_text="start"),
                )
            ),
        )
        actor = spaces.get(task.id).actor
        first = await parked(actor)
        assert (first.status, first.final_output) == ("suspended", None)
        assert set(background_registry.live_background(task.id)) == {"bg-A", "bg-B"}
        gates["A"].set()
        await asyncio.wait_for(saw_first.wait(), 8)
        second = await parked(actor)
        assert (second.id, second.status, second.final_output) == (first.id, "suspended", None)
        assert set(background_registry.live_background(task.id)) == {"bg-B"}
        if last in {"restart", "queued_restart", "delegate_restart"}:
            if last == "queued_restart":
                # 结果已落队列，但原执行者没来得及消费就退出。
                from app.core.tool.background.job import BackgroundConclusion

                await actor.abort()
                assert background_registry.complete(
                    "bg-B", BackgroundConclusion({"output": "already queued"})
                )

            await asyncio.to_thread(background_registry.close)
            await spaces.aclose()
            spaces.bind_loop(asyncio.get_running_loop())
            background_registry.open()
            depends.get_recover_service().recover_after_restart()
            actor = spaces.get(task.id).actor
        elif last == "cancel":
            assert await asyncio.to_thread(
                TransportAssistantService().cancel_tool_call, task.id, "bg-B"
            )
        elif last in {"delegate", "fork"}:
            child_gate.set()
        else:
            gates["B"].set()
        assert await actor.wait_quiescent(8)
        [run] = depends.get_conversation_run_crud().list_by_task(task.id)
        assert (run.id, run.status, run.final_output) == (
            first.id,
            "completed",
            "answer with 2 results",
        )
        rows = depends.get_conversation_task_context_crud().get(task.id)
        statuses = {
            r.message["tool_call_id"]: r.metadata.status
            for r in rows
            if r.message["role"] == "tool"
        }
        assert statuses == {
            "bg-A": "completed",
            "bg-B": "cancelled" if last in {"cancel", "restart"} else "completed",
        }
        assert [
            r.metadata.tool_call_id
            for r in rows
            if r.message["role"] == "user" and r.metadata.kind == "background_result"
        ] == ["bg-A", "bg-B"]
        if last == "queued_restart":
            assert "already queued" in parent_inputs[-1][1]
        if last == "delegate_restart":
            assert "cancelled" in parent_inputs[-1][1]
        if last == "restart":
            assert "backend_restarted" in parent_inputs[-1][1]
        assert [len(results) for results in parent_inputs] == [0, 0, 1, 2]
        assert [
            r.message["content"]
            for r in rows
            if r.message["role"] == "assistant" and not r.message.get("tool_calls")
        ] == [run.final_output]
        assert not depends.get_task_message_queue().pending_nowait(task.id)
    finally:
        child_gate.set()
        for gate in gates.values():
            gate.set()
        await asyncio.to_thread(background_registry.close)
        await spaces.aclose()
        background_registry.open()
