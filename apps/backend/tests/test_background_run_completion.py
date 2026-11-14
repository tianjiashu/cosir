"""真实 actor、ReAct、工具与 SQLite 回投链路；仅模型流和命令进程用可控替身。"""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from app.assistant_transport.service.assistant_transport_service import AssistantTransportService
from app.core.workflow import workflow
from app.core.workflow.branch import session_title
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


@pytest.mark.parametrize(
    "last",
    ["command", "cancel", "restart", "queued_restart"],
)
async def test_background_results_reopen_the_session_after_its_run_answered(backend_env, tmp_path, monkeypatch, last):
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
    session = depends.get_workspace_service().create_session(workspace.id, "parent")
    gates = {name: threading.Event() for name in ("A", "B")}
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

                calls = [
                    {
                        "index": i,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                    for i, (call_id, (name, args)) in enumerate(
                        [("bg-A", cmd("A")), ("bg-B", cmd("B"))]
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
                current = await asyncio.to_thread(
                    depends.get_session_service().require_current_run, session.id
                )
                if current is not None and current.status != "running" and not actor.driving:
                    return current
                await asyncio.sleep(0.02)

    monkeypatch.setattr(execute_terminal, "LocalExecutionBackend", Command)
    monkeypatch.setattr(
        workflow,
        "resolve_chat_model",
        lambda **_: SimpleNamespace(model=Model(), supports_thinking=False),
    )
    monkeypatch.setattr(session_title, "should_launch", lambda *_: False)
    spaces.bind_loop(asyncio.get_running_loop())
    command_results.open()
    try:
        spaces.enqueue(
            session.id,
            SessionMessage.of(
                UserInputBody(
                    text="start",
                    launch=RunLaunch(model_config_id=model.id),
                    presentation=UserInputPresentation(text="start"),
                )
            ),
        )
        actor = spaces.get(session.id).actor
        # 后台工作属于 Session：Run 不等它们，先答复并完成。
        first = await parked(actor)
        assert (first.status, first.final_output) == ("completed", "answer with 0 results")
        assert set(command_results.live_background(session.id)) == {"bg-A", "bg-B"}
        gates["A"].set()
        await asyncio.wait_for(saw_first.wait(), 8)
        # 结论到达时 Session 已空闲：开出新 Run 收尾，另一项工作仍在后台。
        second = await parked(actor)
        assert second.id != first.id
        assert (second.status, second.final_output) == ("completed", "answer with 1 results")
        assert set(command_results.live_background(session.id)) == {"bg-B"}
        if last in {"restart", "queued_restart"}:
            if last == "queued_restart":
                # 结果已落队列，但调度器没来得及处理就退出。
                from app.tools.tool_handler.terminal.command_job import CommandConclusion

                await actor.abort()
                assert command_results.complete(
                    session.id, "bg-B", CommandConclusion({"output": "already queued"})
                )

            await asyncio.to_thread(command_results.close)
            await spaces.aclose()
            spaces.bind_loop(asyncio.get_running_loop())
            command_results.open()
            depends.get_recover_service().recover_after_restart()
            # 只有带可推进消息的 Session 会被恢复物化；重启后空闲的 Session 在这里按需物化。
            actor = spaces.get_or_create(session.id).actor
        elif last == "cancel":
            await asyncio.to_thread(
                AssistantTransportService().cancel_tool_call, session.id, "bg-B"
            )
        else:
            gates["B"].set()
        await wait_session_settled(session.id)
        runs = depends.get_run_crud().list_by_session(session.id)
        rows = depends.get_session_context_crud().get(session.id)
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
        pending = depends.get_session_message_queue().pending_nowait(session.id)
        if last in {"restart", "cancel"}:
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
        for gate in gates.values():
            gate.set()
        await asyncio.to_thread(command_results.close)
        await spaces.aclose()
        command_results.open()
