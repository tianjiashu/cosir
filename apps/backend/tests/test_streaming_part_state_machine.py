"""StreamingPartStateMachine 的增量合并与 part 顺序契约测试。"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from openai.types.chat import ParsedFunctionToolCall

from app.assistant_transport.event import (
    AssistantPartClosedEvent,
    AssistantTextDeltaEvent,
    ToolCallCreatedEvent,
    ToolCallStatusChangedEvent,
)
from app.core.workflow.execution_state.tool_batch import ToolBatch
from app.core.workflow.node_helper import (
    StreamingPartStateMachine,
)
from app.core.workflow.node_helper import streaming_part_state_machine as machine_module


class _Clock:
    """可手动推进的单调时钟，供时间阈值用例注入。"""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        """充当状态机的 ``now`` 回调，返回当前时钟值。"""

        return self.value

    def advance(self, seconds: float) -> None:
        """把时钟向前推进指定秒数。"""

        self.value += seconds


class _Harness:
    """构造状态机并收集它发出的事件，便于按类型断言。"""

    def __init__(
        self,
        *,
        min_chars: int = 32,
        max_interval: float = 0.05,
        clock: _Clock | None = None,
    ) -> None:
        self.clock = clock or _Clock()
        self.events: list[Any] = []
        # stream_writer 在构造期从运行上下文取得，测试用替身提供收集器。
        with patch.object(machine_module, "get_stream_writer", lambda: self.events.append):
            self.machine = StreamingPartStateMachine(task_id=1, run_id=2, step_id="step-1")
        # 合并阈值与时钟是构造期固化的实现细节（生产取 Constant + time.monotonic），测试按用例
        # 白盒注入，才能在字符阈值与时间阈值两个维度上可控。
        self.machine._min_chars = min_chars
        self.machine._max_interval = max_interval
        self.machine._now = self.clock
        self.machine._last_flush_at = self.clock()

    @property
    def deltas(self) -> list[AssistantTextDeltaEvent]:
        """按发出顺序返回增量事件。"""

        return [event for event in self.events if isinstance(event, AssistantTextDeltaEvent)]

    @property
    def closed_parts(self) -> list[str]:
        """按发出顺序返回被收口的 part 类型。"""

        return [event.part for event in self.events if isinstance(event, AssistantPartClosedEvent)]

    def event_kinds(self) -> list[str]:
        """返回事件序列的类型标签（``delta:<part>`` / ``closed:<part>``）。"""

        kinds: list[str] = []
        for event in self.events:
            if isinstance(event, AssistantTextDeltaEvent):
                kinds.append(f"delta:{event.part}")
            elif isinstance(event, AssistantPartClosedEvent):
                kinds.append(f"closed:{event.part}")
        return kinds

    def transcript(self, part: str) -> str:
        """返回某个通道上所有已发出增量的拼接结果。"""

        return "".join(event.delta for event in self.deltas if event.part == part)


def test_merges_deltas_until_char_threshold() -> None:
    """字符数累计到阈值才发出合并增量，未达阈值时不发事件。"""

    harness = _Harness(min_chars=8, max_interval=999.0)

    harness.machine.text("abc")
    harness.machine.text("de")
    assert harness.deltas == []

    harness.machine.text("fgh")
    assert [event.delta for event in harness.deltas] == ["abcdefgh"]

    harness.machine.finish()
    assert harness.event_kinds() == ["delta:text", "closed:text"]
    assert harness.transcript("text") == "abcdefgh"


def test_time_threshold_flushes_below_char_threshold() -> None:
    """字符数未达阈值但超过时间阈值时也要发出，避免短回复迟迟不出字。"""

    harness = _Harness(min_chars=1_000, max_interval=0.05)

    harness.machine.text("ab")
    assert harness.deltas == []

    harness.clock.advance(0.06)
    harness.machine.text("c")

    assert [event.delta for event in harness.deltas] == ["abc"]
    assert harness.closed_parts == []


def test_channel_switch_flushes_and_closes_before_new_channel() -> None:
    """切通道时先发出旧通道缓冲并收口旧 part，之后才累积新通道。"""

    harness = _Harness(min_chars=1_000, max_interval=1_000.0)

    harness.machine.text("hi")
    harness.machine.reasoning("ho")

    assert harness.event_kinds() == ["delta:text", "closed:text"]
    assert harness.transcript("text") == "hi"
    assert harness.transcript("reasoning") == ""

    harness.machine.finish()

    assert harness.event_kinds() == [
        "delta:text",
        "closed:text",
        "delta:reasoning",
        "closed:reasoning",
    ]
    assert harness.transcript("reasoning") == "ho"


def test_tool_call_flushes_pending_before_closing() -> None:
    """tool_call 打断时先把尾部增量发出，再收口 part。"""

    harness = _Harness(min_chars=1_000, max_interval=1_000.0)

    harness.machine.text("tail")
    harness.machine.tool_call([])

    assert harness.event_kinds() == ["delta:text", "closed:text"]
    assert harness.transcript("text") == "tail"


def test_finish_is_idempotent_and_flushes_tail() -> None:
    """finish 发出尾部增量；重复调用不再产生事件。"""

    harness = _Harness(min_chars=1_000, max_interval=1_000.0)

    harness.machine.text("x")
    harness.machine.finish()
    harness.machine.finish()

    assert harness.event_kinds() == ["delta:text", "closed:text"]
    assert harness.transcript("text") == "x"


def test_finish_without_content_emits_nothing() -> None:
    """从未产生内容时 finish 保持静默。"""

    harness = _Harness()

    harness.machine.finish()

    assert harness.events == []


def test_non_positive_threshold_disables_merging() -> None:
    """任一阈值 ≤0 都使该阈值恒满足，合并退化为每次增量立即发出。"""

    chars_disabled = _Harness(min_chars=0)
    chars_disabled.machine.text("a")
    chars_disabled.machine.text("b")
    assert [event.delta for event in chars_disabled.deltas] == ["a", "b"]

    interval_disabled = _Harness(min_chars=1_000, max_interval=0.0)
    interval_disabled.machine.text("a")
    interval_disabled.machine.text("b")
    assert [event.delta for event in interval_disabled.deltas] == ["a", "b"]


def test_merged_events_never_drop_or_duplicate_characters() -> None:
    """合并只改变事件粒度，不改变内容：拼接结果等于输入序列。"""

    harness = _Harness(min_chars=5, max_interval=0.01)
    inputs = ["a", "bb", "ccc", "dddd", "eeeee", "f"]

    for delta in inputs:
        harness.machine.text(delta)
        harness.clock.advance(0.004)
    harness.machine.finish()

    assert len(harness.deltas) < len(inputs)
    assert harness.transcript("text") == "".join(inputs)


def test_part_lifecycle_is_well_formed_across_interleaving() -> None:
    """交错多通道下事件序列合法：只收口已开启的 part，收口后不残留 running。"""

    harness = _Harness(min_chars=3, max_interval=0.02)

    harness.machine.reasoning("ab")
    harness.machine.text("cd")
    harness.clock.advance(0.02)
    harness.machine.reasoning("ef")
    harness.machine.text("gh")
    harness.machine.tool_call([])
    harness.machine.text("ij")
    harness.machine.finish()

    running: set[str] = set()
    for event in harness.events:
        if isinstance(event, AssistantTextDeltaEvent):
            running.add(event.part)
            continue
        assert isinstance(event, AssistantPartClosedEvent)
        assert event.part in running
        running.discard(event.part)

    assert running == set()
    assert harness.transcript("text") == "cdghij"
    assert harness.transcript("reasoning") == "abef"


def _tool_call(call_id: str, tool_name: str) -> ParsedFunctionToolCall:
    """构造 SDK 快照里形态的 function 调用（草稿期 arguments 为空串）。"""

    return ParsedFunctionToolCall(
        id=call_id,
        type="function",
        function={"name": tool_name, "arguments": ""},
    )


def test_cancel_closes_part_then_settles_only_projected_calls(monkeypatch) -> None:
    """取消先收口 text part，再逐条收敛已投影调用；未投影的调用不参与。

    投影判据是「已注册 ∩ 本轮放行」：``write_file`` 虽已注册但不在 ``allows_tools`` 内，
    没有前端 part，因此既不发创建事件也不在取消时收口。
    """

    harness = _Harness(min_chars=1_000, max_interval=1_000.0)
    written: list[Any] = []
    operations = SimpleNamespace(
        vaild_tool_name=lambda name: name in {"read_file", "write_file"},
        all_vaild_tools=[
            SimpleNamespace(name="read_file", display=None),
            SimpleNamespace(name="write_file", display=None),
        ],
        allows_tools=frozenset({"read_file"}),
    )
    runtime = SimpleNamespace(operations=operations, batch=ToolBatch())
    monkeypatch.setattr(machine_module, "_runtime_context", lambda: runtime)
    monkeypatch.setattr(
        machine_module,
        "_message_context",
        lambda: SimpleNamespace(add_message=lambda message, **_: written.append(message)),
    )

    harness.machine.text("hi")
    harness.machine.tool_call(
        [_tool_call("c1", "read_file"), _tool_call("c2", "write_file"), _tool_call("c3", "unknown")]
    )
    assert {
        call_id: call.projection for call_id, call in runtime.batch.valid_tool_calls.items()
    } == {"c1": "projected", "c2": "banned"}
    harness.machine.cancel()

    assert harness.event_kinds() == ["delta:text", "closed:text"]
    created = [event for event in harness.events if isinstance(event, ToolCallCreatedEvent)]
    assert [event.tool_call_id for event in created] == ["c1"]
    cancelled = [event for event in harness.events if isinstance(event, ToolCallStatusChangedEvent)]
    assert [(event.tool_call_id, event.status) for event in cancelled] == [("c1", "cancelled")]
    assert [message["tool_call_id"] for message in written] == ["c1"]


def test_repeated_frames_project_each_call_once_and_reset_starts_new_scope(monkeypatch) -> None:
    """同一请求内重复帧只投影一次；请求边界清空记账后同一 id 才会重新投影。"""

    operations = SimpleNamespace(
        vaild_tool_name=lambda name: name == "read_file",
        all_vaild_tools=[SimpleNamespace(name="read_file", display=None)],
        allows_tools=frozenset({"read_file"}),
    )
    written: list[Any] = []
    runtime = SimpleNamespace(operations=operations, batch=ToolBatch())
    monkeypatch.setattr(machine_module, "_runtime_context", lambda: runtime)
    monkeypatch.setattr(
        machine_module,
        "_message_context",
        lambda: SimpleNamespace(add_message=lambda message, **_: written.append(message)),
    )

    first = _Harness(min_chars=1_000, max_interval=1_000.0)
    # 累积快照：第二帧带着同一个调用再次到达。
    first.machine.tool_call([_tool_call("c1", "read_file")])
    first.machine.tool_call([_tool_call("c1", "read_file")])

    created = [event for event in first.events if isinstance(event, ToolCallCreatedEvent)]
    assert [event.tool_call_id for event in created] == ["c1"]

    # 请求边界：model_node 清空记账，同一 id 属于新请求，必须重新投影。
    runtime.batch = ToolBatch()
    second_events: list[Any] = []
    with patch.object(machine_module, "get_stream_writer", lambda: second_events.append):
        second = StreamingPartStateMachine(task_id=1, run_id=2, step_id="step-2")
    second.tool_call([_tool_call("c1", "read_file")])

    assert [
        event.tool_call_id for event in second_events if isinstance(event, ToolCallCreatedEvent)
    ] == ["c1"]
