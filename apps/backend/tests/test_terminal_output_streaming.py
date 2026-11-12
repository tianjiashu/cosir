"""终端原样增量输出的读取、线程桥接和 snapshot 投影回归测试。"""

from __future__ import annotations

import asyncio
import copy
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.assistant_transport.projection.event_projection import plan_event
from app.assistant_transport.service.conversation_event_projector import (
    ConversationEventProjector,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    empty_snapshot,
    validate_snapshot,
)
from app.assistant_transport.stream import TransportFrame
from app.config.constant import Constant
from app.core.events import (
    RunInitializedEvent,
    RunStatusChangedEvent,
    TerminalCommandStartedData,
    TerminalOutputDeltaData,
    ToolCallCreatedEvent,
    ToolCallRuntimeUpdateEvent,
    ToolCallStatusChangedEvent,
)
from app.core.tool.execute.tool_output_budget import ToolOutputBudget
from app.core.tool.execute.tool_terminal_projection import terminal_event
from app.core.tool.schemas import ToolCall, ToolExecutionContext, ToolObservation
from app.core.workflow.node_helper.tool_batch_runner import run_tool_batch
from app.models.conversation_input import UserInputPresentation
from app.models.message_metadata import BackgroundWork
from app.tools.display.terminal_display import build_terminal_display_data
from app.tools.terminal_runtime.command.command_output import CommandOutput
from app.tools.terminal_runtime.command.execution_result import ExecutionResult
from app.tools.tool_handler.terminal import execute_terminal as execute_terminal_module
from app.tools.tool_handler.terminal.execute_terminal import ExecuteTerminalTool
from app.tools.tool_handler.terminal.live_output_channel import LiveOutputChannel
from tests.runtime_context_stub import runtime_stub
from tests.transport_fixtures import consumed

# RunInitializedEvent 的 created_at 由领域侧从 Run 记录填入；测试给固定值。
_RUN_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _terminal_output_update(
    seq: int,
    text: str,
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
        ),
    )


def _terminal_started_update() -> ToolCallRuntimeUpdateEvent:
    """命令拉起时的初始展示（命令与工作目录），先于任何输出块。"""
    return ToolCallRuntimeUpdateEvent(
        task_id=7,
        run_id=9,
        tool_call_id="call-1",
        seq=0,
        data=TerminalCommandStartedData(
            kind="terminal_command_started",
            display_data={"kind": "terminal-result", "command": "make", "workdir": "/w"},
        ),
    )


def test_command_output_streams_each_chunk_as_it_arrives(tmp_path: Path) -> None:
    emitted: list[str] = []
    output = CommandOutput(
        workspace_root=tmp_path,
        model_budget_chars=Constant.Tools.MAX_OUTPUT_CHARS,
        on_text=emitted.append,
    )

    output.feed(b"starting now\n")
    assert "".join(emitted) == "starting now\n"
    output.feed(b"finished\n")

    output.finish()
    summary = output.summary()
    assert summary.model_head == "starting now\nfinished\n"


def test_command_output_display_keeps_raw_text_without_character_budget(tmp_path: Path) -> None:
    """卡片通道保留原始文本；本用例的长度远小于卡片窗口，窗口裁剪另有单测。"""
    expected = "x" * 200_001
    emitted: list[str] = []
    output = CommandOutput(
        workspace_root=tmp_path,
        model_budget_chars=Constant.Tools.MAX_OUTPUT_CHARS,
        on_text=emitted.append,
    )

    output.feed(expected.encode("utf-8"))

    assert "".join(emitted) == expected
    output.finish()
    assert output.summary().display == expected


def test_finished_command_output_freezes_snapshot_and_closes_live_sink(tmp_path: Path) -> None:
    """封存之后迟到的块不再收集也不再旁路，结果与实时视图停在同一处。"""

    emitted: list[str] = []
    output = CommandOutput(
        workspace_root=tmp_path,
        model_budget_chars=Constant.Tools.MAX_OUTPUT_CHARS,
        on_text=emitted.append,
    )
    output.feed(b"starting now\n")

    output.finish()
    output.feed(b"finished\n")

    assert emitted == ["starting now\n"]
    assert output.summary().display == "starting now\n"


def test_command_output_preserves_ansi_and_sensitive_text_in_stream_and_display(
    tmp_path: Path,
) -> None:
    emitted: list[str] = []
    output = CommandOutput(
        workspace_root=tmp_path,
        model_budget_chars=Constant.Tools.MAX_OUTPUT_CHARS,
        on_text=emitted.append,
    )

    for chunk in [
        b"hello \xe4",
        b"\xbd\xa0\x1b[",
        b"31m token=",
        b"super-secret-value \x1b[0m done\n",
    ]:
        output.feed(chunk)
    output.finish()

    streamed = "".join(emitted)
    assert "hello 你" in streamed
    assert "super-secret-value" in streamed
    assert "token=" in streamed
    assert "\x1b[31m" in streamed
    assert "\x1b[0m" in streamed
    raw = "hello 你\x1b[31m token=super-secret-value \x1b[0m done\n"
    assert output.summary().display == raw
    assert output.summary().model_head == "hello 你 token=super-secret-value  done\n"


def test_terminal_display_preserves_raw_command_and_output() -> None:
    raw_output = "\x1b[31mpassword=visible-secret\x1b[0m\n"

    display_data = build_terminal_display_data(
        command="token=command-secret",
        workdir=Path("/workspace"),
        output=raw_output,
        end="exited",
        exit_code=0,
        signal=None,
        omitted_lines=0,
        log_path="",
    )

    assert display_data["output"] == raw_output
    assert display_data["command"] == "token=command-secret"


def test_execute_terminal_normalizes_model_output_and_keeps_raw_display(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """模型正文只留整理后的纯文本；卡片展示仍是原始文本（含控制序列）。"""
    raw_output = "\x1b[32mapi_key=visible-secret\x1b[0m\n"

    class Backend:
        def stop(self) -> None:
            pass

        def execute(
            self, _command: str, _cwd: str, _timeout: float, output: Any, **_kwargs: Any
        ) -> ExecutionResult:
            output.feed(raw_output.encode())
            output.finish()
            return ExecutionResult(end="exited", exit_code=0)

    monkeypatch.setattr(execute_terminal_module, "LocalExecutionBackend", Backend)
    observation = ExecuteTerminalTool().execute(
        command="echo output",
        execution_context=ToolExecutionContext(
            call_intent="test",
            step_id="step-1",
            task_id=7,
            workspace_id=1,
            workspace_root=tmp_path,
            run_id=1,
            tool_call_id="call-echo",
        ),
    )

    assert "api_key=visible-secret" in observation.content
    assert "\x1b" not in observation.content
    assert observation.display_data is not None
    assert observation.display_data["output"] == raw_output
    assert "output_omitted_lines" not in observation.display_data
    assert "output_log_path" not in observation.display_data


def test_terminal_output_budget_preserves_raw_content_and_artifact(
    tmp_path: Path,
) -> None:
    raw_output = "token=visible-secret\n" + ("x" * 300)
    context = ToolExecutionContext(
        step_id="step-1", task_id=1, workspace_id=1, workspace_root=tmp_path, run_id=1
    )
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
    # artifact 落盘位置必须统一在 workspace 的 `.cosir/tool-artifacts` 下。
    assert artifact_path.startswith(".cosir/tool-artifacts/")
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
            },
        }
    )

    assert isinstance(event.data, TerminalOutputDeltaData)
    assert "truncated" not in event.data.model_dump()
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
async def test_output_channel_batches_thread_output_and_flushes_in_sequence() -> None:
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    events: list[ToolCallRuntimeUpdateEvent] = []
    dispatch_threads: list[int] = []

    def capture(event: ToolCallRuntimeUpdateEvent) -> str:
        events.append(event)
        dispatch_threads.append(threading.get_ident())
        return "stream"

    channel = LiveOutputChannel.open(
        ToolExecutionContext(
            call_intent="test",
            step_id="step-1",
            task_id=7,
            workspace_id=1,
            workspace_root=Path("."),
            run_id=9,
            event_loop=loop,
            event_sink=capture,
            tool_call_id="call-1",
        ),
    )
    assert isinstance(channel, LiveOutputChannel)
    expected = "x" * (Constant.Tools.MAX_OUTPUT_CHARS + 1)
    producer = threading.Thread(target=channel.emit, args=(expected,))
    producer.start()
    producer.join(timeout=1)

    await asyncio.to_thread(channel.finish)

    assert not producer.is_alive()
    streamed = "".join(
        event.data.text for event in events if isinstance(event.data, TerminalOutputDeltaData)
    )
    assert streamed == expected
    assert [event.seq for event in events] == list(range(len(events)))
    assert all(
        len(event.data.text) <= 4096
        for event in events
        if isinstance(event.data, TerminalOutputDeltaData)
    )
    assert dispatch_threads == [loop_thread] * len(events)


@pytest.mark.asyncio
async def test_tool_batch_runs_terminal_off_loop_with_bound_context(tmp_path: Path) -> None:
    """``run_tool_batch`` 在事件循环线程按批次构造执行边界，工具执行发生在循环线程之外。

    通道本身由 ``execute_terminal`` 在工具执行线程按 ``event_loop`` / ``event_sink`` 创建；本用例
    守住这两项与 Run / step 事实确实由批次入口从 ``RuntimeContext`` 取得并透传。
    """
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    received: list[ToolExecutionContext] = []

    class Executor:
        def execute(
            self,
            call: Any,
            execution_context: Any,
            allowed_tool_names: Any,
        ) -> ToolObservation:
            assert threading.get_ident() != loop_thread
            assert allowed_tool_names == {"execute_terminal"}
            received.append(execution_context)
            return ToolObservation(
                tool_name=call.tool_name,
                tool_call_id=call.call_id,
                status="success",
                content="done",
            )

    sink = lambda _event: None  # noqa: E731
    agent = SimpleNamespace()
    rc = runtime_stub(
        tool_executor=Executor(),
        admitted_tools=frozenset({"execute_terminal"}),
        tool_definitions={
            "execute_terminal": SimpleNamespace(parallel_mode="serial", user_decision=None)
        },
        run=SimpleNamespace(id=9, task_id=7),
        task=SimpleNamespace(id=7),
        workspace=SimpleNamespace(id=2, root_path=str(tmp_path)),
        agent=agent,
        event_sink=sink,
    )

    from app.core.workflow.node_helper.common import execution_scope

    with execution_scope(rc, SimpleNamespace()):
        observations = await run_tool_batch(
            rc, [ToolCall(tool_name="execute_terminal", arguments="{}", call_id="call-1")], "step-1"
        )

    assert len(observations) == 1
    [context] = received
    assert (context.task_id, context.run_id, context.workspace_id) == (7, 9, 2)
    assert context.workspace_root == tmp_path
    assert context.step_id == "step-1"
    assert context.agent is agent
    assert context.event_sink is sink
    assert context.event_loop is loop


@pytest.mark.asyncio
async def test_terminal_output_channel_is_built_from_worker_thread(tmp_path: Path) -> None:
    """工具执行线程里必须能按 ``event_loop`` 建出逐调用通道。

    回归：通道创建一度不再接收 ``loop``（改从 ``asyncio.get_running_loop()`` 取），而
    ``LiveOutputChannel.open`` 在 ``asyncio.to_thread`` 的
    工作线程里调用它——工作线程内没有运行中循环，于是每次 ``execute_terminal`` 都抛
    ``TypeError``、只记一条 ERROR 日志，实时输出静默消失。
    """

    loop = asyncio.get_running_loop()
    context = ToolExecutionContext(
        call_intent="test",
        step_id="step-1",
        task_id=7,
        workspace_id=2,
        workspace_root=tmp_path,
        run_id=9,
        event_loop=loop,
        event_sink=lambda _event: None,
    )

    def build_channel() -> Any:
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        return LiveOutputChannel.open(replace(context, tool_call_id="call-1"))

    channel = await asyncio.to_thread(build_channel)
    assert isinstance(channel, LiveOutputChannel)


def test_runtime_update_appends_terminal_output_and_rejects_stale_sequences() -> None:
    state = empty_snapshot()
    projector = ConversationEventProjector(_MemorySnapshotOwner(state))
    projector.process(RunInitializedEvent(created_at=_RUN_CREATED_AT, task_id=7, run_id=9))
    projector.process(consumed(7, 9, 1, UserInputPresentation(text="run tool")))
    projector.process(
        RunStatusChangedEvent(
            changed_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            task_id=7,
            run_id=9,
            status="running",
        )
    )
    projector.process(
        ToolCallCreatedEvent(
            task_id=7,
            run_id=9,
            turn=1,
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
        )
    )

    projector.process(_terminal_started_update())
    projector.process(_terminal_output_update(1, "new"))
    projector.process(_terminal_output_update(0, "stale"))
    projector.process(_terminal_output_update(2, "tail"))

    part = state["messages"][1]["parts"][0]
    assert part["display_data"] == {
        "kind": "terminal-result",
        "command": "make",
        "workdir": "/w",
        "output": "newtail",
    }
    assert part["terminal_output_seq"] == 2


def test_runtime_update_trims_terminal_output_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """连续追加超过卡片窗口后，snapshot 只保留最近一段，并累计省略行数。"""
    monkeypatch.setattr(Constant.Terminal, "DISPLAY_OUTPUT_WINDOW_CHARS", 20)
    state = empty_snapshot()
    projector = ConversationEventProjector(_MemorySnapshotOwner(state))
    projector.process(RunInitializedEvent(created_at=_RUN_CREATED_AT, task_id=7, run_id=9))
    projector.process(consumed(7, 9, 1, UserInputPresentation(text="run tool")))
    projector.process(
        RunStatusChangedEvent(
            changed_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            task_id=7,
            run_id=9,
            status="running",
        )
    )
    projector.process(
        ToolCallCreatedEvent(
            task_id=7, run_id=9, turn=1, tool_call_id="call-1", tool_name="execute_terminal"
        )
    )
    projector.process(
        ToolCallStatusChangedEvent(task_id=7, run_id=9, tool_call_id="call-1", status="running")
    )

    projector.process(_terminal_started_update())
    projector.process(_terminal_output_update(1, "a\n" * 20))
    projector.process(_terminal_output_update(2, "b\n" * 20))

    part = state["messages"][1]["parts"][0]
    assert part["display_data"]["output"] == "b\n" * 9
    assert part["display_data"]["output_omitted_lines"] == 31


def test_background_placeholder_keeps_streaming_after_its_run_suspends() -> None:
    """转到后台的命令在 Run 挂起后仍在输出：只看 part 自己是否还是活的后台占位，不看 Run。"""
    state = empty_snapshot()
    projector = ConversationEventProjector(_MemorySnapshotOwner(state))
    projector.process(RunInitializedEvent(created_at=_RUN_CREATED_AT, task_id=7, run_id=9))
    projector.process(consumed(7, 9, 1, UserInputPresentation(text="run tool")))
    projector.process(
        RunStatusChangedEvent(
            changed_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            task_id=7,
            run_id=9,
            status="running",
        )
    )
    projector.process(
        ToolCallCreatedEvent(
            task_id=7, run_id=9, turn=1, tool_call_id="call-1", tool_name="execute_terminal"
        )
    )
    projector.process(
        ToolCallStatusChangedEvent(task_id=7, run_id=9, tool_call_id="call-1", status="running")
    )
    projector.process(_terminal_started_update())
    projector.process(_terminal_output_update(0, "before "))
    # 用户把前台等待转到后台：占位 pending + background，展示数据不带输出，已流出的输出保留。
    projector.process(
        ToolCallStatusChangedEvent(
            task_id=7,
            run_id=9,
            tool_call_id="call-1",
            status="pending",
            background=BackgroundWork(
                run_id=1,
                kind="terminal_command",
                label="make",
                started_at=_RUN_CREATED_AT.isoformat(),
            ),
            display_data={"kind": "terminal-result", "command": "make", "workdir": "/w"},
        )
    )
    projector.process(
        RunStatusChangedEvent(
            changed_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            task_id=7,
            run_id=9,
            status="suspended",
        )
    )
    projector.process(_terminal_output_update(1, "after"))

    part = state["messages"][1]["parts"][0]
    assert part["display_data"]["output"] == "before after"

    # 结论并入：完整终态写回卡片，实时累计的输出仍是展示上的权威值；之后的迟到块丢弃。
    projector.process(
        ToolCallStatusChangedEvent(
            task_id=7,
            run_id=9,
            tool_call_id="call-1",
            status="completed",
            display_data={
                "kind": "terminal-result",
                "command": "make",
                "workdir": "/w",
                "output": "before after",
                "end": "exited",
                "exit_code": 0,
            },
        )
    )
    projector.process(_terminal_output_update(2, " late"))
    part = state["messages"][1]["parts"][0]
    assert part["status"] == "completed" and "background" in part
    assert part["display_data"]["output"] == "before after"
    assert part["display_data"]["exit_code"] == 0


def test_runtime_update_skips_a_pending_part_that_is_not_a_background_placeholder() -> None:
    state = empty_snapshot()
    projector = ConversationEventProjector(_MemorySnapshotOwner(state))
    projector.process(RunInitializedEvent(created_at=_RUN_CREATED_AT, task_id=7, run_id=9))
    projector.process(consumed(7, 9, 1, UserInputPresentation(text="run tool")))
    projector.process(
        RunStatusChangedEvent(
            changed_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            task_id=7,
            run_id=9,
            status="running",
        )
    )
    projector.process(
        ToolCallCreatedEvent(
            task_id=7, run_id=9, turn=1, tool_call_id="call-1", tool_name="execute_terminal"
        )
    )
    projector.process(_terminal_output_update(0, "early"))

    part = state["messages"][1]["parts"][0]
    assert part.get("display_data") in (None, {}) or "output" not in part["display_data"]


def test_final_terminal_status_uses_authoritative_output_including_unstreamed_tail() -> None:
    state = empty_snapshot()
    projector = ConversationEventProjector(_MemorySnapshotOwner(state))
    projector.process(RunInitializedEvent(created_at=_RUN_CREATED_AT, task_id=7, run_id=9))
    projector.process(consumed(7, 9, 1, UserInputPresentation(text="run tool")))
    projector.process(
        RunStatusChangedEvent(
            changed_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            task_id=7,
            run_id=9,
            status="running",
        )
    )
    projector.process(
        ToolCallCreatedEvent(
            task_id=7,
            run_id=9,
            turn=1,
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
        )
    )

    projector.process(_terminal_started_update())
    streamed_output = "full live output " * 2_000
    for seq, start in enumerate(range(0, len(streamed_output), 4096)):
        projector.process(_terminal_output_update(seq, streamed_output[start : start + 4096]))
    projector.process(
        ToolCallStatusChangedEvent(
            task_id=7,
            run_id=9,
            tool_call_id="call-1",
            status="completed",
            display_data={
                "kind": "terminal-result",
                "output": streamed_output + "final unstreamed tail",
                "exit_code": 0,
            },
        )
    )

    part = state["messages"][1]["parts"][0]
    assert part["display_data"] == {
        "kind": "terminal-result",
        "output": streamed_output + "final unstreamed tail",
        "exit_code": 0,
    }


class _MemorySnapshotOwner:
    """最小 snapshot owner，按 projector mutations 原地更新测试状态。"""

    def __init__(self, state: ConversationStateSnapshot) -> None:
        self.state = state

    def get_state(self, _task_id: int) -> ConversationStateSnapshot:
        return copy.deepcopy(self.state)

    def apply_planned(self, event: Any) -> TransportFrame:
        mutations = tuple(plan_event(event, copy.deepcopy(self.state), {}))
        for mutation in mutations:
            mutation.apply_to(self.state)
        validate_snapshot(self.state)
        return TransportFrame(
            task_id=event.task_id,
            kind="mutation",
            mutations=mutations,
        )


def _streamed_terminal_projector() -> (
    tuple[ConversationStateSnapshot, ConversationEventProjector, str]
):
    """建一个已实时流出终端输出、仍在 running 的 ``execute_terminal`` part。"""
    state = empty_snapshot()
    projector = ConversationEventProjector(_MemorySnapshotOwner(state))
    projector.process(RunInitializedEvent(created_at=_RUN_CREATED_AT, task_id=7, run_id=9))
    projector.process(consumed(7, 9, 1, UserInputPresentation(text="run tool")))
    projector.process(
        RunStatusChangedEvent(
            changed_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            task_id=7,
            run_id=9,
            status="running",
        )
    )
    projector.process(
        ToolCallCreatedEvent(
            task_id=7, run_id=9, turn=1, tool_call_id="call-1", tool_name="execute_terminal"
        )
    )
    projector.process(
        ToolCallStatusChangedEvent(
            task_id=7,
            run_id=9,
            tool_call_id="call-1",
            status="running",
        )
    )
    projector.process(_terminal_started_update())
    streamed_output = "streamed line\n" * 600
    for seq, start in enumerate(range(0, len(streamed_output), 4096)):
        projector.process(_terminal_output_update(seq, streamed_output[start : start + 4096]))
    return state, projector, streamed_output


@pytest.mark.parametrize(
    ("observation", "expected_status", "expected_error"),
    [
        (
            ToolObservation(
                tool_name="execute_terminal",
                status="success",
                content="bounded",
                display_data={
                    "kind": "terminal-result",
                    "output": "authoritative final output",
                    "exit_code": 0,
                },
            ),
            "completed",
            None,
        ),
        (
            ToolObservation(
                tool_name="execute_terminal",
                status="cancelled",
                display_data={"status_hint": "已取消"},
            ),
            "cancelled",
            "已取消",
        ),
        (
            ToolObservation(
                tool_name="execute_terminal",
                status="error",
                content="error: boom",
                display_data={"status_hint": "命令失败"},
            ),
            "failed",
            "命令失败",
        ),
    ],
    ids=["completed", "cancelled", "failed"],
)
def test_terminal_replay_is_idempotent_for_streamed_part(
    observation: ToolObservation, expected_status: str, expected_error: str | None
) -> None:
    """同一观察的终态投两次（实时一次、结算一次）：第二次不改快照，已流出的输出不丢、不叠加。

    两条路径都经 ``terminal_event`` 由同一份观察构造终态，消费方必须按自迁移幂等吸收；终端
    part 的终态会与实时输出合并，是最容易在重放时被覆盖或重复拼接的形态。
    """
    state, projector, streamed_output = _streamed_terminal_projector()

    def replay() -> None:
        projector.process(
            terminal_event(
                observation, task_id=7, run_id=9, step_id="step-1", tool_call_id="call-1"
            )
        )

    replay()
    after_first = copy.deepcopy(state)
    replay()

    assert state == after_first
    part = state["messages"][1]["parts"][0]
    assert part["status"] == expected_status
    assert part.get("error") == expected_error
    assert part["display_data"]["kind"] == "terminal-result"
    assert part["display_data"]["output"] == (
        observation.display_data["output"] if expected_status == "completed" else streamed_output
    )


@pytest.mark.asyncio
async def test_output_channel_publishes_started_display_before_any_output() -> None:
    """启动展示与输出块走同一个事件循环队列：先 ``start`` 的初始展示必然先于输出块到达。"""
    loop = asyncio.get_running_loop()
    events: list[ToolCallRuntimeUpdateEvent] = []
    channel = LiveOutputChannel.open(
        ToolExecutionContext(
            call_intent="test",
            step_id="step-1",
            task_id=7,
            workspace_id=1,
            workspace_root=Path("."),
            run_id=9,
            event_loop=loop,
            event_sink=events.append,
            tool_call_id="call-1",
        ),
    )
    assert isinstance(channel, LiveOutputChannel)
    display = {"kind": "terminal-result", "command": "make", "workdir": "/w"}

    def run_command() -> None:
        channel.start(display)
        channel.emit("first")
        channel.finish()

    await asyncio.to_thread(run_command)
    await asyncio.sleep(0.2)

    assert isinstance(events[0].data, TerminalCommandStartedData)
    assert events[0].data.display_data == display
    assert [
        event.data.text for event in events[1:] if isinstance(event.data, TerminalOutputDeltaData)
    ] == ["first"]


def _running_terminal_projector() -> tuple[ConversationStateSnapshot, ConversationEventProjector]:
    """建一个刚进入 running、还没有任何输出的 ``execute_terminal`` part。"""
    state = empty_snapshot()
    projector = ConversationEventProjector(_MemorySnapshotOwner(state))
    projector.process(RunInitializedEvent(created_at=_RUN_CREATED_AT, task_id=7, run_id=9))
    projector.process(consumed(7, 9, 1, UserInputPresentation(text="run tool")))
    projector.process(
        RunStatusChangedEvent(
            changed_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            task_id=7,
            run_id=9,
            status="running",
        )
    )
    projector.process(
        ToolCallCreatedEvent(
            task_id=7, run_id=9, turn=1, tool_call_id="call-1", tool_name="execute_terminal"
        )
    )
    projector.process(
        ToolCallStatusChangedEvent(
            task_id=7,
            run_id=9,
            tool_call_id="call-1",
            status="running",
            occurred_at=datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC),
        )
    )
    return state, projector


def test_running_projection_records_start_time_from_the_event() -> None:
    state, _ = _running_terminal_projector()

    part = state["messages"][1]["parts"][0]
    assert part["startedAt"] == "2026-01-01T00:00:02+00:00"


def test_started_update_makes_a_card_without_any_output() -> None:
    """没有输出的命令从启动起就有可展开的 terminal-result（命令与工作目录）；重复投递不覆盖。"""
    state, projector = _running_terminal_projector()

    projector.process(_terminal_started_update())
    part = state["messages"][1]["parts"][0]
    assert part["display_data"] == {"kind": "terminal-result", "command": "make", "workdir": "/w"}

    projector.process(_terminal_output_update(0, "out"))
    projector.process(_terminal_started_update())
    assert state["messages"][1]["parts"][0]["display_data"]["output"] == "out"


def test_cancelling_a_command_without_output_keeps_its_command() -> None:
    """取消观察不带成功态展示：没有输出的命令也保留启动时的命令行，卡片不会退化成空行。"""
    state, projector = _running_terminal_projector()
    projector.process(_terminal_started_update())

    projector.process(
        terminal_event(
            ToolObservation(
                tool_name="execute_terminal",
                status="cancelled",
                display_data={"status_hint": "已取消"},
            ),
            task_id=7,
            run_id=9,
            step_id="step-1",
            tool_call_id="call-1",
        )
    )

    part = state["messages"][1]["parts"][0]
    assert part["status"] == "cancelled"
    assert part["display_data"]["kind"] == "terminal-result"
    assert part["display_data"]["command"] == "make"


def test_output_without_a_started_display_is_dropped() -> None:
    """初始展示只会因发布失败而缺失：输出块不再顺手建展示，丢弃并留痕。"""
    state, projector = _running_terminal_projector()

    projector.process(_terminal_output_update(0, "orphan"))

    part = state["messages"][1]["parts"][0]
    assert part.get("display_data") is None
    assert "terminal_output_seq" not in part


def test_duplicate_running_event_keeps_the_original_start_time() -> None:
    state, projector = _running_terminal_projector()

    projector.process(
        ToolCallStatusChangedEvent(
            task_id=7,
            run_id=9,
            tool_call_id="call-1",
            status="running",
            occurred_at=datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC),
        )
    )

    assert state["messages"][1]["parts"][0]["startedAt"] == "2026-01-01T00:00:02+00:00"


def test_leaving_running_clears_the_start_time() -> None:
    """``startedAt`` 只表达「正在运行」：结束后清空，与没有运行中调用的冷重建一致。"""
    state, projector = _running_terminal_projector()
    projector.process(_terminal_started_update())

    projector.process(
        ToolCallStatusChangedEvent(
            task_id=7,
            run_id=9,
            tool_call_id="call-1",
            status="completed",
            display_data={
                "kind": "terminal-result",
                "command": "make",
                "workdir": "/w",
                "output": "",
            },
        )
    )

    assert state["messages"][1]["parts"][0]["startedAt"] is None
