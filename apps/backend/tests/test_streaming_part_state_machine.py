"""StreamingPartStateMachine 的增量合并与 part 顺序契约测试。"""

from typing import Any

from app.assistant_transport.event import (
    AssistantPartClosedEvent,
    AssistantTextDeltaEvent,
)
from app.core.workflows.react.nodes.helper.streaming_part_state_machine import (
    StreamingPartStateMachine,
)


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
        self.machine = StreamingPartStateMachine(
            self.events.append,
            task_id=1,
            run_id=2,
            step_id="step-1",
            text_flush_min_chars=min_chars,
            text_flush_max_interval_seconds=max_interval,
            now=self.clock,
        )

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
    harness.machine.tool_call()

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
    harness.machine.tool_call()
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
