"""工具调用 trace 记录器测试（协议实现、注入链路、降级、callbacks 注入）。

覆盖技术方案第八章测试计划第 3-6 项：假 recorder 注入 ``ToolExecutionService`` 后断言每个 call
对应一次 span 开闭、``record`` 收到对应 observation、事件与消息构造不受影响；缺省（recorder=None）
退化空实现不影响既有行为；``LangfuseToolTraceRecorder`` 在客户端异常时降级为空 observation；
``ReactLikeWorkflow.run`` 把 callbacks 注入 ``graph.astream`` 的 ``config["callbacks"]``。
"""

from contextlib import contextmanager
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.observability.langfuse_tool_trace_recorder import LangfuseToolTraceRecorder
from app.core.workflows.react.workflow import ReactLikeWorkflow
from app.models.enums.event_type import EventType
from app.service.tool_execution.tool_execution_service import ToolExecutionService
from app.tools.schemas import ToolCall, ToolExecutionContext, ToolObservation


class _FakeSpan:
    """假 span：把 ``record`` 收到的 observation 透传给 recorder 收集。"""

    def __init__(self, recorder: "FakeRecorder") -> None:
        self._recorder = recorder

    def record(self, observation: ToolObservation) -> None:
        """记录一次观察结果（供断言）。"""

        self._recorder.recorded.append(observation)


class FakeRecorder:
    """内存实现 ``ToolTraceRecorder`` 协议，用于断言 span 开闭与 observation 传递。"""

    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []
        self.closed: list[tuple[str, str]] = []
        self.recorded: list[ToolObservation] = []

    @contextmanager
    def span(self, call: ToolCall, step_id: str):
        """打开一个记录型 span（进入即记 opened，退出即记 closed）。"""

        self.opened.append((call.call_id, step_id))
        span = _FakeSpan(self)
        try:
            yield span
        finally:
            self.closed.append((call.call_id, step_id))


class _NullSpan:
    """空 span：``record`` 为 no-op（降级路径）。"""

    def record(self, observation: ToolObservation) -> None:
        """空实现：忽略观察结果。"""

        return None


class _BoomRecorder(FakeRecorder):
    """span 内部发生故障时自我降级为 yield 空 span（与 ``LangfuseToolTraceRecorder`` 契约一致）。"""

    @contextmanager
    def span(self, call: ToolCall, step_id: str):
        self.opened.append((call.call_id, step_id))
        try:
            # 模拟内部客户端/上报异常：被 span 自身捕获并降级为空 span，而非向上抛。
            raise RuntimeError("recorder boom")
        except Exception:
            yield _NullSpan()
        finally:
            self.closed.append((call.call_id, step_id))


class FakeScheduler:
    """假调度器：记录调用并把每次调用归一化为成功 observation。"""

    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    def execute(
        self,
        call: ToolCall,
        execution_context: ToolExecutionContext | None = None,
        allowed_tool_names=None,
    ) -> ToolObservation:
        """返回一次成功观察（透传 call_id 便于回绑断言）。"""

        self.calls.append(call)
        return ToolObservation(
            tool_name=call.tool_name,
            status="success",
            content=f"result of {call.tool_name}",
            tool_call_id=call.call_id,
        )


def _call(call_id: str, tool_name: str = "read_file") -> ToolCall:
    """构造一条测试用工具调用。"""

    return ToolCall(tool_name=tool_name, call_id=call_id, arguments={"path": f"/x/{call_id}"})


def _service_with(recorder) -> tuple[ToolExecutionService, FakeScheduler, MagicMock]:
    """构造被测 ``ToolExecutionService`` 与配套的假 scheduler / write_event。"""

    scheduler = FakeScheduler()
    service = ToolExecutionService(
        scheduler=scheduler,
        agent_id="agent-1",
        trace_recorder=recorder,
    )
    write_event = MagicMock()
    return service, scheduler, write_event


def test_service_opens_and_closes_span_per_call() -> None:
    """注入假 recorder：每个 call 对应一次 span 开闭，且 record 收到执行结果。"""

    recorder = FakeRecorder()
    service, scheduler, write_event = _service_with(recorder)

    calls = [_call("c1"), _call("c2")]
    result = service.run_calls_with_events(
        step_id="step-1",
        calls=calls,
        execution_context=None,
        write_event=write_event,
    )

    assert scheduler.calls == calls
    # 每个 call 一次开、一次闭，且顺序对应。
    assert recorder.opened == [("c1", "step-1"), ("c2", "step-1")]
    assert recorder.closed == [("c1", "step-1"), ("c2", "step-1")]
    # record 收到的 observation 与执行结果一致。
    assert [o.tool_call_id for o in recorder.recorded] == ["c1", "c2"]
    # 事件与模型消息构造不受影响。
    assert write_event.call_count == 4  # 每 call：STARTED + FINISHED
    assert len(result.observations) == 2
    assert len(result.messages_for_model) == 2


def test_service_default_null_recorder_regression() -> None:
    """缺省 recorder=None 时退化为空实现，行为与集成前一致（不依赖可观测性）。"""

    service, scheduler, write_event = _service_with(None)

    calls = [_call("c1")]
    result = service.run_calls_with_events(
        step_id="step-1",
        calls=calls,
        execution_context=None,
        write_event=write_event,
    )

    assert scheduler.calls == calls
    assert len(result.observations) == 1
    assert len(result.messages_for_model) == 1
    # STARTED 与 FINISHED 事件仍然照常发出。
    event_types = [c.args[0] for c in write_event.call_args_list]
    assert event_types.count(EventType.TOOL_CALL_STARTED) == 1
    assert event_types.count(EventType.TOOL_CALL_FINISHED) == 1


def test_service_tool_runs_when_recorder_span_raises() -> None:
    """recorder 的 span 进入即抛异常时，工具仍执行、observation 正常产出（降级不中断）。"""

    recorder = _BoomRecorder()
    service, scheduler, write_event = _service_with(recorder)

    calls = [_call("c1")]
    result = service.run_calls_with_events(
        step_id="step-1",
        calls=calls,
        execution_context=None,
        write_event=write_event,
    )

    # 工具执行未受影响。
    assert scheduler.calls == calls
    assert len(result.observations) == 1
    assert result.observations[0].status == "success"


def test_langfuse_recorder_span_degrades_on_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``LangfuseToolTraceRecorder.span`` 在客户端异常时降级为空 observation，不向上抛。"""

    class _BoomClient:
        def start_as_current_observation(self, **kwargs):
            raise RuntimeError("client unavailable")

    class _FakeLangfuse:
        def __new__(cls, *args, **kwargs):
            return _BoomClient()

    langfuse = pytest.importorskip("langfuse")
    monkeypatch.setattr(langfuse, "Langfuse", _FakeLangfuse)

    recorder = LangfuseToolTraceRecorder()
    call = _call("c1")
    # span() 不得抛异常；退出后 flush() 也不得抛异常。
    with recorder.span(call, "step-1") as span:
        span.record(ToolObservation(tool_name="read_file", status="success", content="ok"))
    recorder.flush()


async def test_workflow_injects_callbacks_into_graph_config() -> None:
    """``ReactLikeWorkflow.run(..., callbacks=[sentinel])`` 必须把 sentinel 注入 graph config。"""

    workflow = ReactLikeWorkflow()
    sentinel = object()
    task = MagicMock()
    task.task_id = "task-1"

    operations = MagicMock()
    turn = MagicMock()
    turn.turn_id = "turn-1"
    operations.get_current_turn.return_value = turn

    agent_profile = MagicMock()
    agent_profile.model_name = "deepseek-chat"
    agent_profile.model_settings = MagicMock()
    agent_profile.allowed_tools = set()
    agent_profile.max_steps = 10
    agent_profile.select_tools.return_value = []
    operations.agent_profile = agent_profile
    operations.model_tools = []
    operations.build_messages.return_value = []

    captured: dict = {}

    class _EmptyAsyncIterator:
        """空异步迭代器：使 ``FakeGraph.astream`` 成为合法的 async iterable 但不产出事件。"""

        def __aiter__(self) -> "_EmptyAsyncIterator":
            return self

        async def __anext__(self) -> None:
            raise StopAsyncIteration

    class FakeGraph:
        def astream(self, input_state, config, stream_mode):
            captured["config"] = config
            return _EmptyAsyncIterator()

        async def aget_state(self, config):
            class Snap:
                tasks: ClassVar[list] = []

            return Snap()

    with (
        patch.object(workflow, "_build_graph", return_value=FakeGraph()),
        patch("app.core.workflows.react.workflow.build_chat_model", return_value=MagicMock()),
        patch("app.core.workflows.react.workflow.model_tools_to_langchain", return_value=[]),
        patch("app.core.workflows.react.workflow.runtime_to_langchain", return_value=[]),
        patch("app.core.workflows.react.workflow.build_checkpointer") as bc,
    ):
        checkpointer_cm = AsyncMock()
        bc.return_value = checkpointer_cm
        async for _ in workflow.run(task, operations, callbacks=[sentinel]):
            pass

    assert "config" in captured
    assert sentinel in captured["config"]["callbacks"]
