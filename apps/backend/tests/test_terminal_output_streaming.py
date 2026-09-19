"""终端原样增量输出的读取、线程桥接和 snapshot 投影回归测试。"""

from __future__ import annotations

import asyncio
import copy
import queue
import threading
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from app.assistant_transport.event import (
    RunInitializedEvent,
    RunStatusChangedEvent,
    TerminalOutputDeltaData,
    ToolCallCreatedEvent,
    ToolCallRuntimeUpdateEvent,
    ToolCallStatusChangedEvent,
)
from app.assistant_transport.service.conversation_event_projector import (
    ConversationEventProjector,
)
from app.assistant_transport.event import tool_runtime_output_adapter
from app.assistant_transport.event.tool_runtime_output_adapter import (
    ToolRuntimeOutputChannelFactory,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    empty_snapshot,
    validate_snapshot,
)
from app.assistant_transport.stream import SnapshotChange
from app.config.settings import Settings
from app.core.observability.tool_trace_recorder import _NullToolTraceRecorder
from app.core.tools.display.terminal_display import build_terminal_display_data
from app.core.tools.guard.tool_output_budget import ToolOutputBudget
from app.core.tools.schemas import ToolCall, ToolExecutionContext, ToolObservation
from app.core.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies
from app.core.tools.tool_execute.tool_executor import ToolExecutor
from app.core.tools.tool_execute.tool_handler_runner import ToolHandlerRunner
from app.core.tools.tool_execute.tool_output_channel import (
    BufferedProcessToolOutputChannel,
)
from app.core.tools.tool_handler import execute_terminal as execute_terminal_module
from app.core.tools.tool_handler.execute_terminal import ExecuteTerminalTool
from app.core.tools.tool_handler.terminal.execution_result import ExecutionResult
from app.core.tools.tool_handler.terminal.output_collector import _OutputCollector
from app.core.workflows.workflow_operations import WorkflowOperations


class ChunkStream:
    """按预设顺序返回字节块的 Popen stdout 替身。"""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = iter(chunks)

    def read1(self, _size: int) -> bytes:
        return next(self._chunks, b"")


class PausingStream:
    """首个块后暂停后续读取，允许断言 sink 在 EOF 前收到输出。"""

    def __init__(self) -> None:
        self.next_read_started = threading.Event()
        self.release = threading.Event()
        self._index = 0

    def read1(self, _size: int) -> bytes:
        self._index += 1
        if self._index == 1:
            return b"starting now\n"
        if self._index == 2:
            self.next_read_started.set()
            self.release.wait(timeout=2)
            return b"finished\n"
        return b""


def _terminal_output_update(
    seq: int,
    text: str,
    *,
    truncated: bool = False,
) -> ToolCallRuntimeUpdateEvent:
    """Construct one typed terminal-output payload carried by the runtime-update event."""
    return ToolCallRuntimeUpdateEvent(
        task_id=7,
        run_id=9,
        tool_call_id="call-1",
        seq=seq,
        data=TerminalOutputDeltaData(
            kind="terminal_output_delta",
            text=text,
            truncated=truncated,
        ),
    )


def test_output_collector_emits_before_process_output_eof() -> None:
    stream = PausingStream()
    emitted: list[tuple[str, bool]] = []
    collector = _OutputCollector(
        stream, sink=lambda text, truncated: emitted.append((text, truncated))
    )
    reader = threading.Thread(target=collector.run)
    reader.start()

    try:
        assert stream.next_read_started.wait(timeout=1)
        assert "".join(text for text, _ in emitted) == "starting now\n"
    finally:
        stream.release.set()
        reader.join(timeout=2)

    assert not reader.is_alive()
    assert collector.get() == "starting now\nfinished\n"


def test_output_collector_stream_does_not_apply_character_budget() -> None:
    expected = "x" * (Settings.MAX_TOOL_OUTPUT_CHARS + 1)
    emitted: list[str] = []
    collector = _OutputCollector(
        ChunkStream([expected.encode("utf-8")]),
        sink=lambda text, _truncated: emitted.append(text),
        stream_budget=0,
    )

    collector.run()

    assert "".join(emitted) == expected
    assert collector.get() == expected
    assert collector.truncated is False


def test_sealed_output_collector_freezes_snapshot_and_closes_live_sink() -> None:
    stream = PausingStream()
    emitted: list[tuple[str, bool]] = []
    collector = _OutputCollector(
        stream, sink=lambda text, cut: emitted.append((text, cut))
    )
    reader = threading.Thread(target=collector.run)
    reader.start()
    assert stream.next_read_started.wait(timeout=1)

    collector.seal()
    stream.release.set()
    reader.join(timeout=2)

    assert not reader.is_alive()
    assert emitted == [("starting now\n", False), ("", True)]
    assert collector.truncated is True
    assert "starting now\n" in collector.get()
    assert "finished" not in collector.get()
    assert "additional output unavailable" in collector.get()


def test_output_collector_preserves_ansi_and_sensitive_text_in_stream_and_final_output() -> (
    None
):
    emitted: list[str] = []
    collector = _OutputCollector(
        ChunkStream(
            [
                b"hello \xe4",
                b"\xbd\xa0\x1b[",
                b"31m token=",
                b"super-secret-value \x1b[0m done\n",
            ]
        ),
        sink=lambda text, _truncated: emitted.append(text),
    )

    collector.run()

    streamed = "".join(emitted)
    assert "hello 你" in streamed
    assert "super-secret-value" in streamed
    assert "token=" in streamed
    assert "\x1b[31m" in streamed
    assert "\x1b[0m" in streamed
    assert collector.get() == "hello 你\x1b[31m token=super-secret-value \x1b[0m done\n"


def test_terminal_display_preserves_raw_command_and_output() -> None:
    raw_output = "\x1b[31mpassword=visible-secret\x1b[0m\n"

    display_data = build_terminal_display_data(
        command="token=command-secret",
        workdir=Path("/workspace"),
        output=raw_output,
        exit_code=0,
        timed_out=False,
        truncated=False,
    )

    assert display_data["output"] == raw_output
    assert display_data["command"] == "token=command-secret"


def test_execute_terminal_returns_unmodified_output_to_model_and_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw_output = "\x1b[32mapi_key=visible-secret\x1b[0m\n"

    class Backend:
        def execute(self, *_args: Any, **_kwargs: Any) -> ExecutionResult:
            return ExecutionResult(
                output=raw_output,
                exit_code=0,
                timed_out=False,
                truncated=False,
            )

    monkeypatch.setattr(
        execute_terminal_module, "create_backend", lambda _name: Backend()
    )
    observation = ExecuteTerminalTool().execute(
        command="echo output",
        execution_context=SimpleNamespace(workspace_root=tmp_path),
    )

    assert raw_output in observation.content
    assert observation.display_data is not None
    assert observation.display_data["output"] == raw_output


def test_terminal_output_budget_preserves_raw_content_and_artifact(
    tmp_path: Path,
) -> None:
    raw_output = "token=visible-secret\n" + ("x" * 300)
    context = ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=tmp_path)
    observation = ToolObservation(
        tool_name="execute_terminal",
        status="success",
        content=raw_output,
    )

    result = ToolOutputBudget(max_chars=200).apply(observation, context)

    assert "token=visible-secret" in (result.content or "")
    assert result.artifact_data is not None
    artifact_path = result.artifact_data["artifact_path"]
    assert isinstance(artifact_path, str) and artifact_path
    assert (tmp_path / artifact_path).read_text(encoding="utf-8") == raw_output


def test_runtime_update_event_uses_a_strict_payload_discriminator() -> None:
    event = ToolCallRuntimeUpdateEvent.model_validate(
        {
            "task_id": 7,
            "run_id": 9,
            "tool_call_id": "call-1",
            "seq": 0,
            "data": {
                "kind": "terminal_output_delta",
                "text": "output",
                "truncated": False,
            },
        }
    )

    assert isinstance(event.data, TerminalOutputDeltaData)
    with pytest.raises(ValidationError):
        ToolCallRuntimeUpdateEvent.model_validate(
            {
                "task_id": 7,
                "run_id": 9,
                "tool_call_id": "call-1",
                "seq": 0,
                "data": {"kind": "unknown", "text": "output"},
            }
        )


@pytest.mark.asyncio
async def test_output_channel_batches_thread_output_and_flushes_in_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    events: list[ToolCallRuntimeUpdateEvent] = []
    dispatch_threads: list[int] = []

    def capture(event: ToolCallRuntimeUpdateEvent) -> str:
        events.append(event)
        dispatch_threads.append(threading.get_ident())
        return "stream"

    monkeypatch.setattr(
        tool_runtime_output_adapter, "dispatch_conversation_event", capture
    )
    channel = ToolRuntimeOutputChannelFactory().create(
        task_id=7,
        run_id=9,
        tool_call_id="call-1",
        tool_name="execute_terminal",
        loop=loop,
    )
    assert isinstance(channel, BufferedProcessToolOutputChannel)
    expected = "x" * (Settings.MAX_TOOL_OUTPUT_CHARS + 1)
    producer = threading.Thread(target=channel.emit, args=(expected,))
    producer.start()
    producer.join(timeout=1)

    await asyncio.to_thread(channel.finish)

    assert not producer.is_alive()
    streamed = "".join(
        event.data.text
        for event in events
        if isinstance(event.data, TerminalOutputDeltaData)
    )
    assert streamed == expected
    assert [event.seq for event in events] == list(range(len(events)))
    assert all(
        len(event.data.text) <= 4096
        for event in events
        if isinstance(event.data, TerminalOutputDeltaData)
    )
    assert all(
        not event.data.truncated
        for event in events
        if isinstance(event.data, TerminalOutputDeltaData)
    )
    assert dispatch_threads == [loop_thread] * len(events)


@pytest.mark.asyncio
async def test_workflow_places_event_loop_in_run_scoped_tool_dependencies(
    tmp_path: Path,
) -> None:
    loop = asyncio.get_running_loop()
    context = ToolExecutionContext(
        task_id=7,
        workspace_id=2,
        workspace_root=tmp_path,
        run_id=9,
        runtime_dependencies=ToolRuntimeDependencies(),
    )
    operations = WorkflowOperations.__new__(WorkflowOperations)
    operations._current_run = SimpleNamespace(id=9)
    operations._execution_context = context
    operations._allowed_tool_names = frozenset({"execute_terminal"})
    operations._parallel_mode_by_name = {"execute_terminal": "serial"}

    class Executor:
        def execute(
            self,
            call: Any,
            execution_context: Any = None,
            allowed_tool_names: Any = None,
        ) -> ToolObservation:
            assert execution_context.runtime_dependencies.runtime_event_loop is loop
            assert allowed_tool_names == {"execute_terminal"}
            return ToolObservation(
                tool_name=call.tool_name,
                tool_call_id=call.call_id,
                status="success",
                content="done",
            )

    operations._executor = cast(ToolExecutor, Executor())
    operations._trace_recorder = _NullToolTraceRecorder()

    result = await operations.run_tool_calls(
        task_id=7,
        calls=[ToolCall(tool_name="execute_terminal", arguments={}, call_id="call-1")],
    )

    assert len(result.observations) == 1
    assert operations._execution_context.runtime_dependencies.runtime_event_loop is loop


def test_output_queue_completion_waits_for_tail_and_reports_dropped_chunks() -> None:
    result_queue: queue.Queue[tuple[str, dict[str, object]]] = queue.Queue()
    result_queue.put(("success", {"result": "done"}))
    output_queue: queue.Queue[tuple[object, ...]] = queue.Queue()
    output_queue.put(("delta", "last chunk", False))
    release_completion = threading.Event()
    received: list[tuple[str, bool]] = []

    def sink(text: str, truncated: bool) -> None:
        received.append((text, truncated))
        if text == "last chunk":
            release_completion.set()

    def produce_completion() -> None:
        assert release_completion.wait(timeout=1)
        output_queue.put(("complete", True))

    producer = threading.Thread(target=produce_completion)
    producer.start()

    class RunningProcess:
        def is_alive(self) -> bool:
            return True

    result = ToolHandlerRunner._wait_for_result(
        RunningProcess(),
        result_queue,
        timeout=1,
        output_queue=output_queue,
        output_sink=sink,
    )
    producer.join(timeout=1)

    assert result == ("success", {"result": "done"})
    assert not producer.is_alive()
    assert received == [("last chunk", False), ("", True)]


def test_handler_process_backpressures_full_queue_without_dropping_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.tools.tool_execute import tool_handler_runner as runner_module

    monkeypatch.setattr(runner_module.os, "setsid", lambda: None, raising=False)
    monkeypatch.setattr(
        runner_module, "assign_current_process_to_kill_on_close_job", lambda: None
    )
    emitted = threading.Event()
    release = threading.Event()

    class ClosableQueue(queue.Queue[object]):
        def close(self) -> None:
            return None

        def join_thread(self) -> None:
            return None

    def handler(*, output_sink: Any, execution_context: Any) -> str:
        output_sink("accepted", False)
        output_sink("second chunk", False)
        emitted.set()
        assert release.wait(timeout=2)
        return "done"

    result_queue = ClosableQueue(maxsize=1)
    output_queue = ClosableQueue(maxsize=1)
    worker = threading.Thread(
        target=ToolHandlerRunner._execute_handler,
        args=(handler, {}, result_queue, None, None, output_queue),
    )
    worker.start()
    first_item = output_queue.get(timeout=1)
    assert emitted.wait(timeout=1)
    second_item = output_queue.get(timeout=1)
    release.set()
    worker.join(timeout=2)
    completion_item = output_queue.get(timeout=1)

    assert not worker.is_alive()
    assert first_item == ("delta", "accepted", False)
    assert second_item == ("delta", "second chunk", False)
    assert completion_item == ("complete", False)


def test_runtime_update_appends_terminal_output_and_rejects_stale_sequences() -> None:
    state = empty_snapshot()
    projector = ConversationEventProjector(_MemorySnapshotOwner(state))
    projector.process(RunInitializedEvent(task_id=7, run_id=9))
    projector.process(RunStatusChangedEvent(task_id=7, run_id=9, status="running"))
    projector.process(
        ToolCallCreatedEvent(
            task_id=7,
            run_id=9,
            tool_call_id="call-1",
            tool_name="execute_terminal",
        )
    )
    projector.process(
        ToolCallStatusChangedEvent(
            task_id=7,
            run_id=9,
            tool_call_id="call-1",
            status="running",
            args={"command": "echo live"},
        )
    )

    projector.process(_terminal_output_update(1, "new"))
    projector.process(_terminal_output_update(0, "stale"))
    projector.process(_terminal_output_update(2, "", truncated=True))

    part = state["runs"][0]["messages"][1]["parts"][0]
    assert part["display_data"] == {
        "kind": "terminal-result",
        "output": "new",
        "stream_truncated": True,
    }
    assert part["terminal_output_seq"] == 2


def test_final_terminal_status_preserves_complete_streamed_display_output() -> None:
    state = empty_snapshot()
    projector = ConversationEventProjector(_MemorySnapshotOwner(state))
    projector.process(RunInitializedEvent(task_id=7, run_id=9))
    projector.process(RunStatusChangedEvent(task_id=7, run_id=9, status="running"))
    projector.process(
        ToolCallCreatedEvent(
            task_id=7,
            run_id=9,
            tool_call_id="call-1",
            tool_name="execute_terminal",
        )
    )
    projector.process(
        ToolCallStatusChangedEvent(
            task_id=7,
            run_id=9,
            tool_call_id="call-1",
            status="running",
            args={"command": "emit a lot"},
        )
    )

    streamed_output = "full live output " * 2_000
    for seq, start in enumerate(range(0, len(streamed_output), 4096)):
        projector.process(
            _terminal_output_update(seq, streamed_output[start : start + 4096])
        )
    projector.process(
        ToolCallStatusChangedEvent(
            task_id=7,
            run_id=9,
            tool_call_id="call-1",
            status="completed",
            display_data={
                "kind": "terminal-result",
                "output": "bounded final observation",
                "truncated": True,
                "exit_code": 0,
            },
        )
    )

    part = state["runs"][0]["messages"][1]["parts"][0]
    assert part["display_data"] == {
        "kind": "terminal-result",
        "output": streamed_output,
        "truncated": False,
        "stream_truncated": False,
        "exit_code": 0,
    }


class _MemorySnapshotOwner:
    """最小 snapshot owner，按 projector mutations 原地更新测试状态。"""

    def __init__(self, state: ConversationStateSnapshot) -> None:
        self.state = state

    def get_state(self, _task_id: int) -> ConversationStateSnapshot:
        return copy.deepcopy(self.state)

    def apply_planned(self, event: Any) -> SnapshotChange:
        mutations = tuple(event.plan(copy.deepcopy(self.state)))
        for mutation in mutations:
            parent: Any = self.state
            if not mutation.path:
                parent.clear()
                parent.update(copy.deepcopy(mutation.value))
                continue
            for key in mutation.path[:-1]:
                parent = parent[key]
            key = mutation.path[-1]
            if mutation.kind == "append-text":
                parent[key] += mutation.value
            elif isinstance(parent, list) and key == len(parent):
                parent.append(copy.deepcopy(mutation.value))
            else:
                parent[key] = copy.deepcopy(mutation.value)
        validate_snapshot(self.state)
        return SnapshotChange(event.task_id, copy.deepcopy(self.state), mutations)
