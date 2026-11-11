"""真实 actor、ReAct、工具与 SQLite 回投链路；仅模型流和命令进程用可控替身。"""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from app.assistant_transport.service.transport_assistant_service import TransportAssistantService
from app.core.tool.background.background_registry import background_registry
from app.core.workflow import workflow
from app.core.workflow.branch import task_title
from app.core.workflow.message_queue.task_message import RunLaunch, TaskMessage, UserInputBody
from app.models.conversation_input import UserInputPresentation
from app.service import depends
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces as spaces
from app.tools.terminal_runtime.command.execution_result import ExecutionResult
from app.tools.tool_handler.terminal import execute_terminal
from tests.model_config_fixtures import create_model_config
from tests.sdk_fixtures import sdk_events


@pytest.mark.parametrize(
    "last",
    ["command", "delegate", "fork", "cancel", "restart", "queued_restart", "delegate_restart"],
)
async def test_background_results_reopen_the_task_after_its_run_answered(backend_env, tmp_path, monkeypatch, last):
    model = create_model_config(
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
    task = depends.get_workspace_service().create_task(workspace.id, "parent")
    gates = {name: threading.Event() for name in ("A", "B")}
    child_gate = asyncio.Event()
    saw_first = asyncio.Event()
    parent_inputs = []

    class Command:
        def __init__(self):
            self.stopped = threading.Event()
            self.command = None

        def execute(self, command, cwd, timeout, output, **kwargs):
            self.command = command
            if not self.stopped.is_set():
                assert gates[command].wait(10), "command was never released"
            output.feed(command.encode())
            output.finish()
            if self.stopped.is_set():
                return ExecutionResult(end="cancelled")
            return ExecutionResult(end="exited", exit_code=0)

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
                            "intent": label,
                            "command": label,
                            "background": True,
                        }

                    second = {
                        "delegate": (
                            "delegate_task",
                            {
                                "intent": "review",
                                "child_agent_id": "general-assistant",
                                "agent_name": "review",
                                "message": "child-report",
                            },
                        ),
                        "fork": (
                            "fork_task",
                            {"intent": "fork", "title": "review", "message": "child-report"},
                        ),
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
                    text="start",
                    launch=RunLaunch(model_config_id=model.id),
                    presentation=UserInputPresentation(text="start"),
                )
            ),
        )
        actor = spaces.get(task.id).actor
        # 后台工作属于 Task：Run 不等它们，先答复并完成。
        first = await parked(actor)
        assert (first.status, first.final_output) == ("completed", "answer with 0 results")
        assert set(background_registry.live_background(task.id)) == {"bg-A", "bg-B"}
        gates["A"].set()
        await asyncio.wait_for(saw_first.wait(), 8)
        # 结论到达时 Task 已空闲：开出新 Run 收尾，另一项工作仍在后台。
        second = await parked(actor)
        assert second.id != first.id
        assert (second.status, second.final_output) == ("completed", "answer with 1 results")
        assert set(background_registry.live_background(task.id)) == {"bg-B"}
        if last in {"restart", "queued_restart", "delegate_restart"}:
            if last == "queued_restart":
                # 结果已落队列，但调度器没来得及处理就退出。
                from app.core.tool.background.background_job import BackgroundConclusion

                await actor.abort()
                assert background_registry.complete(
                    task.id, "bg-B", BackgroundConclusion({"output": "already queued"})
                )

            await asyncio.to_thread(background_registry.close)
            await spaces.aclose()
            spaces.bind_loop(asyncio.get_running_loop())
            background_registry.open()
            depends.get_recover_service().recover_after_restart()
            # 只有带可推进消息的 Task 会被恢复物化；重启后空闲的 Task 在这里按需物化。
            actor = spaces.get_or_create(task.id).actor
        elif last == "cancel":
            assert await asyncio.to_thread(
                TransportAssistantService().cancel_tool_call, task.id, "bg-B"
            )
        elif last in {"delegate", "fork"}:
            child_gate.set()
        else:
            gates["B"].set()
        assert await actor.wait_quiescent(8)
        runs = depends.get_conversation_run_crud().list_by_task(task.id)
        rows = depends.get_conversation_task_context_crud().get(task.id)
        statuses = {
            r.message["tool_call_id"]: r.metadata.status
            for r in rows
            if r.message["role"] == "tool"
        }
        result_call_ids = [
            r.metadata.tool_call_id
            for r in rows
            if r.message["role"] == "user" and r.metadata.kind == "background_result"
        ]
        final_answers = [
            r.message["content"]
            for r in rows
            if r.message["role"] == "assistant" and not r.message.get("tool_calls")
        ]
        pending = depends.get_task_message_queue().pending_nowait(task.id)
        if last in {"restart", "delegate_restart", "cancel"}:
            # 重启只收敛（占位推「已中断」、留一条通知）；单停只落定卡片、回执留给下一次推进。都不开新 Run。
            assert [(r.status, r.final_output) for r in runs] == [
                ("completed", "answer with 0 results"),
                ("completed", "answer with 1 results"),
            ]
            assert statuses == {"bg-A": "completed", "bg-B": "cancelled"}
            assert result_call_ids == ["bg-A"]
            assert [len(results) for results in parent_inputs] == [0, 0, 1]
            assert final_answers == ["answer with 0 results", "answer with 1 results"]
            expected_kind = "background_result" if last == "cancel" else "system_notice"
            assert [message.kind for message in pending] == [expected_kind]
        else:
            assert [(r.status, r.final_output) for r in runs] == [
                ("completed", "answer with 0 results"),
                ("completed", "answer with 1 results"),
                ("completed", "answer with 2 results"),
            ]
            assert statuses == {"bg-A": "completed", "bg-B": "completed"}
            assert result_call_ids == ["bg-A", "bg-B"]
            if last == "queued_restart":
                assert "already queued" in parent_inputs[-1][1]
            assert [len(results) for results in parent_inputs] == [0, 0, 1, 2]
            assert final_answers == [r.final_output for r in runs]
            assert pending == []
    finally:
        child_gate.set()
        for gate in gates.values():
            gate.set()
        await asyncio.to_thread(background_registry.close)
        await spaces.aclose()
        background_registry.open()
