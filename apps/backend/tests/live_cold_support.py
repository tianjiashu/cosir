"""实时快照与冷重建的差分夹具：真实 actor、workflow、SQLite、projector，只替换模型流。

同一事实有实时投影与冷重建两条派生路径，它们的漂移只有跑完整链路才看得出，这里把两条路径的
结果规范化后逐项比较。``usage``、``durationMs`` 等只在展示时用到、两边允许有秒级差异的字段不比。
"""

import asyncio
import copy
import json
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import update

from app.config.configuration import get_tool_registry
from app.core.tool.results.tool_awaiting_decision import tool_awaiting_decision
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import ToolDefinition
from app.core.tool.schemas.tool_execution_context import ToolExecutionContext
from app.core.workflow import workflow
from app.core.workflow.branch import session_title
from app.core.workflow.message_queue.session_message import (
    AgentMessageBody,
    RunLaunch,
    SessionMessage,
    UserInputBody,
)
from app.models.conversation_input import MessageSource, UserInputPresentation
from app.models.run_record import RunRecord
from app.models.session_record import SessionRecord
from app.service import depends
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces as spaces
from app.storage.model.session_model import SessionModel
from app.storage.store_engines import main_db_factory
from app.tools.terminal_runtime.command.execution_result import ExecutionResult
from app.tools.tool_handler.terminal import execute_terminal
from app.tools.tool_handler.terminal.command_results import CommandResultRegistry, command_results
from tests.model_config_fixtures import create_model_config
from tests.sdk_fixtures import sdk_events


def tool_call(
    call_id: str, name: str, args: dict[str, Any] | str, index: int = 0
) -> dict[str, Any]:
    """流式 delta 里的一个工具调用；参数给字符串时原样透传，用来构造残缺 JSON。"""
    arguments = args if isinstance(args, str) else json.dumps(args)
    return {
        "index": index,
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


class ScriptedModel:
    """按步回放模型响应：一步是 ``(deltas, finish_reason[, 流中异常])``、异常或异步生成器工厂。"""

    def __init__(self, steps: Sequence[Any]) -> None:
        self.steps = list(steps)
        self.requests: list[list[dict[str, Any]]] = []

    async def stream(self, messages: Sequence[dict[str, Any]]) -> AsyncIterator[Any]:
        self.requests.append(copy.deepcopy(list(messages)))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            async for frame in step():
                yield frame
            return
        # 流中异常替代收尾帧。必须逐帧取：sdk_events 复用同一个快照对象，提前取会看到未来的状态。
        for index, frame in enumerate(sdk_events(step[0], finish_reason=step[1])):
            if len(step) > 2 and index == len(step[0]):
                raise step[2]
            yield frame

    async def close(self) -> None:
        pass


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _hitl(*, execution_context: ToolExecutionContext) -> Any:
    if execution_context.user_decision is None:
        return tool_awaiting_decision(
            "probe_hitl", execution_context.tool_call_id, {"kind": "probe"}
        )
    return tool_success(
        "probe_hitl",
        f"decided {execution_context.user_decision.decision}",
        tool_call_id=execution_context.tool_call_id,
    )


def _install_probe_tools(session: SessionRecord) -> None:
    """注册一个需要用户决定的探针工具，并把它固化进 Session 的模型 schema。"""
    registry = get_tool_registry()
    if registry.get_tool_definition("probe_hitl") is None:
        registry.register(
            ToolDefinition(
                name="probe_hitl",
                group="probe",
                description="probe",
                handler=_hitl,
                args_model=_NoArgs,
                user_decision="execute",
            )
        )
    definition = registry.get_tool_definition("probe_hitl")
    assert definition is not None
    schemas = [*session.tool_schemas, definition.to_model_tool_definition()]
    with main_db_factory().begin() as db:
        db.execute(
            update(SessionModel).where(SessionModel.id == session.id).values(tool_schemas=schemas)
        )


class GatedCommands:
    """替换终端执行器：命令按名字阻塞在闸门上，用例决定它何时结束。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
        self.gates = {name: threading.Event() for name in names}
        self.started: list[str] = []
        gates, started = self.gates, self.started

        class Backend:
            def __init__(self) -> None:
                self.stopped = threading.Event()
                self.command: str | None = None

            def execute(
                self, command: str, cwd: Any, timeout: Any, output: Any, **_: Any
            ) -> ExecutionResult:
                self.command = command
                started.append(command)
                if not self.stopped.is_set():
                    assert gates[command].wait(10), "command was never released"
                output.feed(f"out:{command}".encode())
                output.finish()
                if self.stopped.is_set():
                    return ExecutionResult(end="cancelled")
                return ExecutionResult(end="exited", exit_code=0)

            def stop(self) -> None:
                self.stopped.set()
                if self.command:
                    gates[self.command].set()

        monkeypatch.setattr(execute_terminal, "LocalExecutionBackend", Backend)

    def release(self) -> None:
        for gate in self.gates.values():
            gate.set()


class LiveColdEnv:
    """一个工作区加一个 Session；保持订阅，让实时快照一直是投影出来的工作副本。"""

    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: Sequence[Any]
    ) -> None:
        self.model = ScriptedModel(steps)
        self.model_config = create_model_config(
            name="test",
            base_url="http://127.0.0.1",
            api_key="test",
            model_name="test",
            context_window_k=96,
            supports_thinking=True,
            supports_reasoning_effort=True,
            supports_image=False,
        )
        self.workspace = depends.get_workspace_service().create_workspace("ws", str(tmp_path))
        self.session = depends.get_workspace_service().create_session(self.workspace.id, "parent")
        _install_probe_tools(self.session)
        monkeypatch.setattr(
            workflow,
            "resolve_chat_model",
            lambda **_: SimpleNamespace(model=self.model, supports_thinking=True),
        )
        monkeypatch.setattr(session_title, "should_launch", lambda *_: False)
        spaces.bind_loop(asyncio.get_running_loop())
        command_results.open()
        self.state = depends.get_conversation_state_service()
        _, self._unsubscribe, _ = self.state.subscribe_with_snapshot(self.session.id)

    def send(
        self,
        text: str = "start",
        *,
        delivery: str | None = None,
        session_id: int | None = None,
    ) -> None:
        kwargs = {"delivery": delivery} if delivery else {}
        launch = RunLaunch(
            model_config_id=self.model_config.id
        )
        body = UserInputBody(
            text=text, launch=launch, presentation=UserInputPresentation(text=text)
        )
        spaces.enqueue(session_id or self.session.id, SessionMessage.of(body, **kwargs))

    def send_agent(self, text: str) -> None:
        body = AgentMessageBody(
            text=text,
            launch=RunLaunch(model_config_id=self.model_config.id),
            source=MessageSource(session_id=4, name="上级"),
        )
        spaces.enqueue(self.session.id, SessionMessage.of(body))

    async def parked(self, session_id: int | None = None) -> RunRecord:
        """等到当前 Run 离开运行态且没有执行者。"""
        return await self._wait(session_id, lambda run: run.status != "running")

    async def until(self, status: str, session_id: int | None = None) -> RunRecord:
        return await self._wait(session_id, lambda run: run.status == status)

    async def _wait(
        self, session_id: int | None, matches: Callable[[RunRecord], bool]
    ) -> RunRecord:
        target = session_id or self.session.id
        async with asyncio.timeout(8):
            while True:
                actor = spaces.get(target).actor
                run = await asyncio.to_thread(
                    depends.get_session_service().require_current_run, target
                )
                if run is not None and matches(run) and not actor.driving:
                    return run
                await asyncio.sleep(0.02)

    def live(self) -> dict[str, Any]:
        return copy.deepcopy(self.state.get_state(self.session.id))

    def cold(self, session_id: int | None = None) -> dict[str, Any]:
        return copy.deepcopy(self.state._rebuild(session_id or self.session.id))

    def assert_parity(self) -> None:
        assert normalized(self.live()) == normalized(self.cold())

    async def close(self) -> None:
        self._unsubscribe()
        await asyncio.to_thread(command_results.close)
        await spaces.aclose()
        command_results.open()


def registered(registry: CommandResultRegistry, session_id: int, tool_call_id: str) -> bool:
    """这次调用是否有活条目（前台或后台）。"""
    live = [*registry.live_background(session_id), *registry.foreground_calls(session_id)]
    return tool_call_id in live


def tool_parts(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        part
        for message in state["messages"]
        for part in message["parts"]
        if part["type"] == "tool-call"
    ]


def _part(part: dict[str, Any]) -> dict[str, Any]:
    kept = {k: v for k, v in part.items() if v not in (None, False, [], {}) or k == "status"}
    kept.pop("terminal_output_seq", None)
    return kept


def normalized(state: dict[str, Any]) -> dict[str, Any]:
    messages = []
    for message in state["messages"]:
        outcome = message["outcome"]
        if outcome:
            outcome = {k: v for k, v in outcome.items() if k not in ("durationMs", "usage")}
        messages.append(
            {
                "id": message["id"],
                "role": message["role"],
                "turn": message["turn"],
                "inherited": message["inherited"],
                "outcome": outcome,
                "parts": [_part(part) for part in message["parts"]],
            }
        )
    activity = state["activity"]
    if activity:
        activity = {k: v for k, v in activity.items() if k != "usage"}
    return {"messages": messages, "activity": activity, "background": state["background_tasks"]}
