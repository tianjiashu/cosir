"""协作取消只在模型读取边界生效；真实 SDK stream 仍由执行者关闭。"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError, AsyncOpenAI

from app.config.constant import Constant
from app.core.llm_provider.openai_model import OpenAIChatModel
from app.core.workflow.cancel.conversation_run_cancellation_registry import (
    ConversationRunCancellationRegistry,
)
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.models.enums.react_route import ReactRoute
from app.core.workflow.node_helper import streaming_part_state_machine as streaming_module
from app.core.workflow.node_helper.common import RunStopped
from app.core.workflow.nodes import model_node, structured_output_node
from app.models.enums.conversation_run_status import RunTransition
from tests.runtime_context_stub import run_state_stub, runtime_stub


class BlockingBody(httpx.AsyncByteStream):
    """用事件控制网络返回和关闭，不依赖 sleep 猜测取消时序。"""

    content_type = "text/event-stream"

    def __init__(self, *, first_chunk=False, boundary="chunk", hold_close=False):
        self.first_chunk = first_chunk
        self.boundary = boundary
        self.reading = asyncio.Event()
        self.resume_read = asyncio.Event()
        self.closing = asyncio.Event()
        self.closed = asyncio.Event()
        self.release_close = asyncio.Event()
        if not hold_close:
            self.release_close.set()
        self.read_task = None
        self.close_task = None

    @staticmethod
    def frame(content, finish_reason=None):
        return (
            "data: "
            + json.dumps(
                {
                    "id": "request-1",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "test",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": finish_reason,
                            "delta": {"role": "assistant", "content": content},
                        }
                    ],
                }
            )
            + "\n\n"
        ).encode()

    async def __aiter__(self):
        self.read_task = asyncio.current_task()
        if self.first_chunk:
            yield self.frame("{}")
        self.reading.set()
        await self.resume_read.wait()
        if self.boundary == "timeout":
            raise httpx.ReadTimeout("read timeout")
        if self.boundary == "chunk":
            yield self.frame("ignored after cancellation", "stop")
        if self.boundary == "complete":
            yield self.frame("", "stop")

    async def aclose(self):
        self.close_task = asyncio.current_task()
        self.closing.set()
        await self.release_close.wait()
        self.closed.set()


class BlockingCompletionBody(BlockingBody):
    """非流式响应必须整体读完；取消标记不会中断 SDK 的读取。"""

    content_type = "application/json"

    async def __aiter__(self):
        self.read_task = asyncio.current_task()
        self.reading.set()
        await self.resume_read.wait()
        if self.boundary == "timeout":
            raise httpx.ReadTimeout("read timeout")
        yield json.dumps(
            {
                "id": "request-1",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "{}"},
                    }
                ],
            }
        ).encode()


def sdk_model(body):
    return OpenAIChatModel(
        AsyncOpenAI(
            api_key="test",
            max_retries=0,
            timeout=120,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        200, headers={"content-type": body.content_type}, stream=body
                    )
                )
            ),
        ),
        {"model": "test"},
    )


def bind_node(monkeypatch, model):
    """只替换节点的存储和展示边界，网络读取使用真实 SDK。"""
    registry = ConversationRunCancellationRegistry()
    snapshots, memberships, cancellations = [], [], []

    def save(snapshot, **kwargs):
        snapshots.append(snapshot)
        memberships.append(kwargs["include_in_context"])

    context = SimpleNamespace(
        load_message=lambda: [],
        save_model_response=save,
    )

    def transition_run(_run_id, transition, **kwargs):
        if transition is RunTransition.CANCEL:
            cancellations.append(kwargs)
        return SimpleNamespace(status=transition.target.value)

    runtime = runtime_stub(
        run=SimpleNamespace(id=7, task_id=1),
        task=SimpleNamespace(id=1, workspace_id=1),
        model=model,
        usage_stats=ConversationRunUsageStats(),
        run_state=run_state_stub(transition=transition_run),
        task_space=SimpleNamespace(take_runtime_notices=lambda **kw: None),
    )
    runtime.is_cancelled = lambda: registry.is_cancelled(7)
    for module in (model_node, structured_output_node):
        monkeypatch.setattr(module, "_runtime_context", lambda: runtime)
    monkeypatch.setattr(model_node, "_message_context", lambda: context)
    monkeypatch.setattr(streaming_module, "_runtime_context", lambda: runtime)
    monkeypatch.setattr(streaming_module, "_message_context", lambda: context)
    monkeypatch.setattr(structured_output_node, "_message_context", lambda: context)
    monkeypatch.setattr(streaming_module, "emit", lambda event: None)
    state = ReactExecutionState(
        step_count=0,
        step_limit=3,
        next_node=ReactRoute.MODEL,
    )
    return SimpleNamespace(
        registry=registry,
        state=state,
        runtime=runtime,
        context=context,
        snapshots=snapshots,
        memberships=memberships,
        cancellations=cancellations,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("first_chunk", [False, True])
@pytest.mark.parametrize("boundary", ["chunk", "end", "timeout"])
async def test_user_cancel_waits_for_sdk_boundary_without_masking_errors(
    monkeypatch, first_chunk, boundary
):
    body = BlockingBody(first_chunk=first_chunk, boundary=boundary, hold_close=True)
    model = sdk_model(body)
    bound = bind_node(monkeypatch, model)
    driver = asyncio.create_task(model_node._model_node(bound.state))
    try:
        await asyncio.wait_for(body.reading.wait(), 1)
        await asyncio.to_thread(bound.registry.request_cancel, 7)
        assert not driver.done()
        assert len(bound.snapshots) == int(first_chunk)
        assert not body.closed.is_set()
        assert not bound.cancellations
        body.resume_read.set()
        await asyncio.wait_for(body.closing.wait(), 1)
        bound.registry.request_cancel(7)
        assert not driver.done()
        assert not bound.cancellations
        body.release_close.set()
        with pytest.raises(httpx.ReadTimeout if boundary == "timeout" else RunStopped):
            await asyncio.wait_for(driver, 1)
        assert body.closed.is_set()
        assert body.read_task is body.close_task is driver
        assert len(bound.cancellations) == (0 if boundary == "timeout" else 1)
        assert [s.choices[0].message.content for s in bound.snapshots] == (
            ["{}"] if first_chunk else []
        )
        assert bound.memberships == [False] * len(bound.snapshots)
    finally:
        body.release_close.set()
        if not driver.done():
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)
        await model.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_at", ["reading", "closing"])
@pytest.mark.parametrize("boundary", ["complete", "end", "timeout"])
async def test_cancel_settles_after_stream_close_and_part_flush(monkeypatch, cancel_at, boundary):
    """清理顺序不变：显式取消才收口 cancelled，超时原样上抛，均不提升草稿。"""
    body = BlockingBody(first_chunk=True, boundary=boundary, hold_close=True)
    model = sdk_model(body)
    bound = bind_node(monkeypatch, model)
    order = []
    monkeypatch.setattr(Constant.Workflow, "DEFAULT_TEXT_FLUSH_MIN_CHARS", 10000)
    monkeypatch.setattr(Constant.Workflow, "DEFAULT_TEXT_FLUSH_MAX_INTERVAL_SECONDS", 3600)

    def project(event):
        assert body.closed.is_set()
        order.append(type(event).__name__)

    original_cancel_tools = streaming_module.StreamingPartStateMachine.cancel

    def cancel_tools(self):
        assert order == []
        result = original_cancel_tools(self)
        assert order == ["AssistantTextDeltaEvent", "AssistantPartClosedEvent"]
        order.append("cancel_tools")
        return result

    original_transition_run = bound.runtime.run_state.transition

    def cancel_run(run_id, transition, **kwargs):
        if transition is RunTransition.CANCEL:
            assert order[-1] == "cancel_tools"
            order.append("cancel_run")
        return original_transition_run(run_id, transition, **kwargs)

    monkeypatch.setattr(streaming_module, "emit", project)
    monkeypatch.setattr(streaming_module.StreamingPartStateMachine, "cancel", cancel_tools)
    bound.runtime.run_state.transition = cancel_run
    driver = asyncio.create_task(model_node._model_node(bound.state))
    try:
        await asyncio.wait_for(body.reading.wait(), 1)
        if cancel_at == "reading":
            bound.registry.request_cancel(7)
        body.resume_read.set()
        await asyncio.wait_for(body.closing.wait(), 1)
        if cancel_at == "closing":
            bound.registry.request_cancel(7)
        assert not driver.done()
        assert order == []
        assert not bound.cancellations
        body.release_close.set()
        with pytest.raises(httpx.ReadTimeout if boundary == "timeout" else RunStopped):
            await asyncio.wait_for(driver, 1)
        assert order == ["AssistantTextDeltaEvent", "AssistantPartClosedEvent"] + (
            [] if boundary == "timeout" else ["cancel_tools", "cancel_run"]
        )
        assert len(bound.cancellations) == (0 if boundary == "timeout" else 1)
        assert bound.snapshots
        assert bound.memberships == [False] * len(bound.snapshots)
    finally:
        body.release_close.set()
        if not driver.done():
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)
        await model.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_timeout_before_headers_is_failure_even_when_cancel_requested(monkeypatch, cancelled):
    reading, resume_read = asyncio.Event(), asyncio.Event()

    async def handler(request):
        reading.set()
        await resume_read.wait()
        raise httpx.ReadTimeout("header timeout", request=request)

    model = OpenAIChatModel(
        AsyncOpenAI(
            api_key="test",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ),
        {"model": "test"},
    )
    bound = bind_node(monkeypatch, model)
    driver = asyncio.create_task(model_node._model_node(bound.state))
    try:
        await asyncio.wait_for(reading.wait(), 1)
        if cancelled:
            await asyncio.to_thread(bound.registry.request_cancel, 7)
        assert not driver.done()
        resume_read.set()
        with pytest.raises(APITimeoutError):
            await asyncio.wait_for(driver, 1)
        assert not bound.cancellations
        assert bound.memberships == [False] * len(bound.snapshots)
    finally:
        await model.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("user_cancel_first", [False, True])
async def test_executor_cancellation_still_waits_for_sdk_close(monkeypatch, user_cancel_first):
    body = BlockingBody(first_chunk=True, hold_close=True)
    model = sdk_model(body)
    bound = bind_node(monkeypatch, model)
    driver = asyncio.create_task(model_node._model_node(bound.state))
    try:
        await asyncio.wait_for(body.reading.wait(), 1)
        if user_cancel_first:
            bound.registry.request_cancel(7)
        driver.cancel()
        await asyncio.wait_for(body.closing.wait(), 1)
        assert not driver.done()
        body.release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await driver
        assert body.closed.is_set()
        assert body.read_task is body.close_task is driver
        assert not bound.cancellations
        assert bound.memberships == [False] * len(bound.snapshots)
    finally:
        body.release_close.set()
        await model.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["complete", "timeout"])
async def test_completed_run_structured_branch_ignores_late_cancel(monkeypatch, boundary):
    body = BlockingCompletionBody(boundary=boundary)
    model = sdk_model(body)
    bound = bind_node(monkeypatch, model)
    bound.runtime.agent = SimpleNamespace(
        structured_output=SimpleNamespace(name="result", json_schema={"type": "object"}),
        max_steps=10,
    )
    bound.runtime.structured_output_messages = [{"role": "assistant", "content": "自然回答"}]
    updates = []
    bound.runtime.run_state.update_completed_result = lambda _run_id, **kwargs: updates.append(
        kwargs
    )
    driver = asyncio.create_task(structured_output_node._structured_output_node(bound.state))
    try:
        await asyncio.wait_for(body.reading.wait(), 1)
        await asyncio.to_thread(bound.registry.request_cancel, 7)
        assert not driver.done()
        assert bound.snapshots == []
        assert not body.closed.is_set()
        body.resume_read.set()
        result = await asyncio.wait_for(driver, 1)
        assert result["next_node"] == ReactRoute.END
        assert body.closed.is_set()
        # 主线已 completed，迟到的 Run 取消信号不能改变支线结果或重新迁移状态。
        assert not bound.cancellations
        assert len(updates) == 1
        if boundary == "complete":
            assert updates[0]["final_output"] == "{}"
        else:
            assert updates[0]["end_reason"] == "structured_output_failed"
    finally:
        await model.close()


@pytest.mark.asyncio
async def test_cancel_during_input_resolution_does_not_open_request(monkeypatch):
    body = BlockingBody()
    model = sdk_model(body)
    bound = bind_node(monkeypatch, model)

    def load_message():
        # 附件解析已收口到 ``load_message``：取消发生在取上下文这条边界上。
        bound.registry.request_cancel(7)
        return []

    monkeypatch.setattr(bound.context, "load_message", load_message)
    try:
        with pytest.raises(RunStopped):
            await model_node._model_node(bound.state)
        assert not body.reading.is_set()
        assert len(bound.cancellations) == 1
    finally:
        await model.close()


@pytest.mark.asyncio
async def test_model_timeout_keeps_persisted_snapshot_excluded_from_context(monkeypatch):
    """网络失败前已有持久化输出，关闭流不会重复追加或提升为模型上下文。"""
    body = BlockingBody(first_chunk=True, boundary="timeout", hold_close=True)
    model = sdk_model(body)
    bound = bind_node(monkeypatch, model)
    driver = asyncio.create_task(model_node._model_node(bound.state))
    try:
        await asyncio.wait_for(body.reading.wait(), 1)
        assert len(bound.snapshots) == 1
        assert bound.memberships == [False]
        body.resume_read.set()
        await asyncio.wait_for(body.closing.wait(), 1)
        assert len(bound.snapshots) == 1
        body.release_close.set()
        with pytest.raises(httpx.ReadTimeout):
            await asyncio.wait_for(driver, 1)
        assert body.closed.is_set()
        assert [snapshot.choices[0].message.content for snapshot in bound.snapshots] == ["{}"]
        assert bound.memberships == [False]
        assert not bound.cancellations
    finally:
        body.release_close.set()
        if not driver.done():
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)
        await model.close()
