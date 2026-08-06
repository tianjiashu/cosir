"""终端命令输出实时流式推送链路测试。

覆盖三层：
- 采集层 ``_OutputCollector``：按行回传、ANSI 剥离、脱敏、流式预算截断、sink 异常隔离；
- IPC 层 ``ToolExecutor``：跨进程输出队列 drain 与尾部片段不丢失；
- 编排层 ``ToolExecutionService._build_output_sink``：片段经事件总线广播为不持久化的
  ``TOOL_OUTPUT_DELTA``、实时通道不可用时的降级、以及广播失败时的隔离。
"""

import contextlib
import io
import multiprocessing
import queue
import tempfile
import time
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from pydantic import BaseModel

from app.models.enums.event_type import EventType
from app.models.payload import ToolOutputDeltaPayload
from app.service.tool_execution.tool_execution_service import ToolExecutionService
from app.tools.schemas import ToolCall, ToolDefinition, ToolExecutionContext
from app.tools.tool_execute.tool_executor import ToolExecutor
from app.tools.tool_handler.terminal.local_backend import _OutputCollector


def _collect(lines: list[bytes], stream_budget: int = 0) -> list[tuple[str, bool]]:
    """跑一次采集器并返回 sink 收到的全部片段。

    参数:
        lines: 模拟子进程输出的原始字节行。
        stream_budget: 实时通道字符预算；``<=0`` 表示不限。

    返回:
        ``(text, truncated)`` 片段列表，按回传顺序。
    """
    chunks: list[tuple[str, bool]] = []
    collector = _OutputCollector(
        io.BytesIO(b"".join(lines)),
        sink=lambda text, truncated: chunks.append((text, truncated)),
        stream_budget=stream_budget,
    )
    collector.run()
    return chunks


class TestOutputCollectorStreaming:
    """采集层实时回传行为。"""

    def test_emits_each_line_and_keeps_final_output(self) -> None:
        """每读到一行即回传，且不影响最终完整输出。"""
        chunks: list[tuple[str, bool]] = []
        collector = _OutputCollector(
            io.BytesIO(b"first\nsecond\n"),
            sink=lambda text, truncated: chunks.append((text, truncated)),
        )
        collector.run()

        assert [text for text, _ in chunks] == ["first\n", "second\n"]
        assert collector.get() == "first\nsecond\n"

    def test_strips_ansi_before_emitting(self) -> None:
        """回传前剥离 ANSI 转义序列，保证前端拿到可读文本。"""
        chunks = _collect([b"\x1b[31mred\x1b[0m\n"])

        assert chunks == [("red\n", False)]

    def test_redacts_secrets_before_emitting(self) -> None:
        """回传前完成凭据脱敏，避免 secret 经实时通道外泄。"""
        chunks = _collect([b"export API_KEY=super-secret-value-1234\n"])

        assert len(chunks) == 1
        assert "super-secret-value-1234" not in chunks[0][0]

    def test_stops_emitting_after_stream_budget_exhausted(self) -> None:
        """预算耗尽后截断当前片段并永久关闭实时通道。"""
        chunks = _collect([b"aaaaa\n", b"bbbbb\n", b"ccccc\n"], stream_budget=8)

        assert chunks[0] == ("aaaaa\n", False)
        assert chunks[1] == ("bb", True)
        assert len(chunks) == 2

    def test_sink_exception_does_not_break_collection(self) -> None:
        """sink 抛错只关闭实时通道，最终输出仍完整可用。"""

        def _boom(text: str, truncated: bool) -> None:
            raise RuntimeError("sink down")

        collector = _OutputCollector(io.BytesIO(b"one\ntwo\n"), sink=_boom)
        collector.run()

        assert collector.get() == "one\ntwo\n"

    def test_no_sink_is_noop(self) -> None:
        """未提供 sink 时采集器行为与既有实现一致。"""
        collector = _OutputCollector(io.BytesIO(b"plain\n"))
        collector.run()

        assert collector.get() == "plain\n"


class TestDrainOutputQueue:
    """IPC 层队列 drain 行为。"""

    def test_drains_all_ready_chunks(self) -> None:
        """一次 drain 取空当前已积压的全部片段。"""
        output_queue: multiprocessing.Queue = multiprocessing.Queue()
        output_queue.put(("a", False))
        output_queue.put(("b", True))

        received: list[tuple[str, bool]] = []
        _drain_until(output_queue, lambda t, tr: received.append((t, tr)), expected=2)

        assert received == [("a", False), ("b", True)]

    def test_noop_without_queue_or_sink(self) -> None:
        """队列或 sink 缺一时直接返回，不抛异常。"""
        received: list[str] = []
        ToolExecutor._drain_output_queue(None, lambda t, tr: received.append(t))
        ToolExecutor._drain_output_queue(multiprocessing.Queue(), None)

        assert received == []

    def test_sink_exception_stops_drain_silently(self) -> None:
        """sink 抛错时静默结束本次 drain，不向上传播。"""
        output_queue: multiprocessing.Queue = multiprocessing.Queue()
        output_queue.put(("x", False))

        def _boom(text: str, truncated: bool) -> None:
            raise RuntimeError("sink down")

        ToolExecutor._drain_output_queue(output_queue, _boom)


def _drain_until(
    output_queue: "multiprocessing.Queue",
    sink: Any,
    expected: int,
) -> None:
    """反复 drain 直到 sink 收到期望数量的片段。

    ``multiprocessing.Queue.put`` 由 feeder 线程异步刷入管道，且 macOS 不支持
    ``qsize()``，因此用「重试 drain」而非查询队列长度来等待元素就绪。

    参数:
        output_queue: 承载片段的跨进程队列。
        sink: 片段消费回调，由调用方统计收到的数量。
        expected: 期望收到的片段总数。

    返回:
        无。

    异常:
        AssertionError: 1 秒内仍未收齐。

    副作用:
        消费队列中的片段并调用 ``sink``。
    """
    received = 0

    def _counting_sink(text: str, truncated: bool) -> None:
        nonlocal received
        received += 1
        sink(text, truncated)

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        ToolExecutor._drain_output_queue(output_queue, _counting_sink)
        if received >= expected:
            return
        time.sleep(0.01)
    raise AssertionError(f"片段未在超时内就绪，expected={expected}, received={received}")


def _echo_handler(text: str, execution_context: Any = None, output_sink: Any = None) -> str:
    """测试用 handler：把 text 按行经 output_sink 回传后返回终态输出。

    参数:
        text: 待"输出"的文本，按 ``\\n`` 切分为行。
        execution_context: 执行上下文（本 handler 不使用）。
        output_sink: 实时输出回调；由 ``ToolExecutor`` 子进程入口注入。

    返回:
        终态完整输出文本。
    """
    for line in text.splitlines(keepends=True):
        if output_sink is not None:
            output_sink(line, False)
        time.sleep(0.01)
    return text


class _EchoArgs(BaseModel):
    """``_echo_handler`` 的参数校验契约。"""

    text: str


def _make_tool(
    name: str, execution_mode: Literal["thread", "process"] = "thread"
) -> ToolDefinition:
    """构造一个绑定 ``_echo_handler`` 的测试工具定义。

    参数:
        name: 工具名。
        execution_mode: 隔离执行模式，决定走子进程还是当前线程。

    返回:
        可直接交给 ``ToolExecutor.execute`` 的工具定义。
    """
    return ToolDefinition(
        name=name,
        description="test",
        permission="read",
        handler=_echo_handler,
        args_model=_EchoArgs,
        timeout_seconds=30,
        execution_mode=execution_mode,
    )


class TestToolExecutorProcessStreaming:
    """process 模式端到端：子进程片段经队列实时到达父进程 sink。"""

    def test_streams_chunks_and_returns_final_result(self) -> None:
        """运行期片段全部到达 sink，且终态结果不受影响。"""
        tool = _make_tool("echo_stream", execution_mode="process")
        received: list[str] = []
        observation = ToolExecutor().execute(
            tool,
            {"text": "l1\nl2\nl3\n"},
            output_sink=lambda text, truncated: received.append(text),
            tool_call_id="call-x",
        )

        assert observation.status == "success"
        assert observation.content == "l1\nl2\nl3\n"
        assert "".join(received) == "l1\nl2\nl3\n"

    def test_thread_mode_ignores_output_sink(self) -> None:
        """thread 模式不建队列、不回调 sink，仍正常返回结果。"""
        tool = _make_tool("echo_thread")
        received: list[str] = []
        observation = ToolExecutor().execute(
            tool,
            {"text": "only\n"},
            output_sink=lambda text, truncated: received.append(text),
        )

        assert observation.status == "success"
        assert received == []


class _RecordingBus:
    """记录所有 publish 调用的事件总线替身。"""

    def __init__(self) -> None:
        """初始化空事件列表。"""
        self.events: list[Any] = []

    def publish(self, event: Any) -> None:
        """记录一条被广播的事件。

        参数:
            event: 被广播的 ``RuntimeEvent``。

        返回:
            无。

        副作用:
            追加到 ``self.events``。
        """
        self.events.append(event)


class _ImmediateLoop:
    """把 ``call_soon_threadsafe`` 退化为同步直调的事件循环替身。"""

    def __init__(self, fail: bool = False) -> None:
        """初始化替身。

        参数:
            fail: 为 True 时 ``call_soon_threadsafe`` 抛 ``RuntimeError``，
                模拟事件循环已关闭的场景。
        """
        self.fail = fail

    def call_soon_threadsafe(self, callback: Any, *args: Any) -> None:
        """同步执行回调（或按配置抛错）。

        参数:
            callback: 待调度的可调用对象。
            *args: 传给回调的位置参数。

        返回:
            无。

        异常:
            RuntimeError: ``fail=True`` 时抛出，模拟循环已关闭。
        """
        if self.fail:
            raise RuntimeError("event loop is closed")
        callback(*args)


def _make_service(bus: Any) -> ToolExecutionService:
    """构造一个仅用于测试 sink 构建的执行服务。

    参数:
        bus: 注入的事件总线（或替身）。

    返回:
        已注入总线的 ``ToolExecutionService``。
    """
    return ToolExecutionService(scheduler=cast(Any, None), agent_id="agent-1", event_bus=bus)


class TestBuildOutputSink:
    """编排层片段 → 不持久化广播事件。"""

    @staticmethod
    def _call() -> ToolCall:
        """构造一个终端工具调用。"""
        return ToolCall(tool_name="execute_terminal", arguments={"command": "ls"}, call_id="call-1")

    @staticmethod
    def _context(task_id: str = "task-1", turn_id: str = "turn-1") -> ToolExecutionContext:
        """构造带 task/turn 边界的执行上下文。

        参数:
            task_id: 任务标识；传空串用于验证边界缺失时的降级。
            turn_id: 轮次标识；传空串用于验证边界缺失时的降级。

        返回:
            可交给 ``_build_output_sink`` 的执行上下文。
        """
        return ToolExecutionContext(
            task_id=task_id,
            turn_id=turn_id,
            workspace_id="ws-1",
            workspace_root=str(Path(tempfile.gettempdir()) / "ws"),
        )

    def test_publishes_tool_output_delta_event(self) -> None:
        """片段被广播为携带 call_id / step_id 的 TOOL_OUTPUT_DELTA 事件。"""
        bus = _RecordingBus()
        sink = _make_service(bus)._build_output_sink(
            "step-1", self._call(), self._context(), cast(Any, _ImmediateLoop())
        )
        assert sink is not None

        sink("hello\n", False)

        assert len(bus.events) == 1
        event = bus.events[0]
        assert event.event_type is EventType.TOOL_OUTPUT_DELTA
        assert event.task_id == "task-1"
        assert event.turn_id == "turn-1"
        payload = event.payload
        assert isinstance(payload, ToolOutputDeltaPayload)
        assert payload.tool_call_id == "call-1"
        assert payload.step_id == "step-1"
        assert payload.text == "hello\n"
        assert payload.truncated is False

    def test_propagates_truncated_flag(self) -> None:
        """预算耗尽标记随事件透传给前端。"""
        bus = _RecordingBus()
        sink = _make_service(bus)._build_output_sink(
            "step-1", self._call(), self._context(), cast(Any, _ImmediateLoop())
        )
        assert sink is not None

        sink("tail", True)

        assert bus.events[0].payload.truncated is True

    def test_returns_none_without_bus_loop_or_context(self) -> None:
        """总线 / 事件循环 / 执行上下文缺一时不构造回调，直接禁用实时通道。"""
        loop = cast(Any, _ImmediateLoop())
        context = self._context()

        assert _make_service(None)._build_output_sink("s", self._call(), context, loop) is None
        assert (
            _make_service(_RecordingBus())._build_output_sink("s", self._call(), context, None)
            is None
        )
        assert (
            _make_service(_RecordingBus())._build_output_sink("s", self._call(), None, loop) is None
        )
        assert (
            _make_service(_RecordingBus())._build_output_sink(
                "s", self._call(), self._context(task_id="", turn_id=""), loop
            )
            is None
        )

    def test_closed_loop_failure_is_isolated(self) -> None:
        """事件循环已关闭时不向上抛出，避免反压命令执行。"""
        sink = _make_service(_RecordingBus())._build_output_sink(
            "step-1", self._call(), self._context(), cast(Any, _ImmediateLoop(fail=True))
        )
        assert sink is not None

        sink("x", False)  # 不应抛出


class TestQueueContract:
    """队列容量契约：满时丢弃而非阻塞。"""

    def test_put_nowait_raises_full_when_saturated(self) -> None:
        """有界队列写满时抛 Full，由子进程侧 suppress 丢弃该片段。

        队列容量由信号量即时占用（不依赖 feeder 线程刷盘），因此 maxsize=1 时
        第二次 ``put_nowait`` 必然立即抛 ``queue.Full``。
        """
        output_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=1)
        output_queue.put_nowait(("a", False))

        with pytest.raises(queue.Full):
            output_queue.put_nowait(("b", False))

    def test_child_sink_suppresses_full_queue(self) -> None:
        """子进程 sink 在队列写满时静默丢弃片段，不抛出、不阻塞。"""
        output_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=1)

        def _child_sink(text: str, truncated: bool) -> None:
            with contextlib.suppress(Exception):
                output_queue.put_nowait((text, truncated))

        _child_sink("a", False)
        _child_sink("b", False)  # 队列已满，静默丢弃
