"""直接验证业务循环边界，不依赖图框架的节点重放。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.core.workflow.workflow as module
from app.core.workflow.execution_state.react_route import ReactRoute
from app.models.conversation_run_progress import ConversationRunProgress
from app.models.enums.conversation_run_status import RunTransition
from tests.runtime_context_stub import run_state_stub, runtime_stub
from tests.sdk_fixtures import sdk_completion


@pytest.fixture
def loop(monkeypatch):
    progress = []
    calls = []
    context = SimpleNamespace(
        begin_run=lambda *args: None,
        load_message=lambda: [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "自然回答"},
        ],
    )
    model = SimpleNamespace(close=AsyncMock())
    run = SimpleNamespace(id=1, task_id=2, execution_data=ConversationRunProgress(), usage=None)
    operations = runtime_stub(
        run=run,
        task=SimpleNamespace(id=run.task_id),
        agent=SimpleNamespace(max_steps=10, structured_output=None, model=None),
        allows_tools=frozenset({"review"}),
        run_state=run_state_stub(
            save_execution=lambda _run_id, value: progress.append(value),
            transition=lambda _run_id, transition, **k: calls.append(transition.name.lower()),
        ),
        task_space=SimpleNamespace(task_tool_schemas=(), get_context_manager=lambda **k: context),
    )
    async def prepare_input(state):
        """inbox 并入节点替身：不触碰真实 inbox，只记录经过并流向 model。"""
        if not operations.cancelled:
            calls.append("queue")
        return {"next_node": ReactRoute.MODEL}

    monkeypatch.setattr(module, "_prepare_input_node", prepare_input)
    monkeypatch.setattr(
        module,
        "resolve_chat_model",
        lambda **kw: SimpleNamespace(model=model, supports_thinking=False),
    )
    return SimpleNamespace(
        run=run, ops=operations, progress=progress, calls=calls, model=model, context=context
    )


@pytest.mark.asyncio
async def test_queue_only_at_model_boundary_and_end_stops_driving(loop, monkeypatch):
    count = 0

    async def model(state):
        nonlocal count
        count += 1
        loop.calls.append("model")
        return {
            "step_count": count,
            "next_node": ReactRoute.TOOLS if count == 1 else ReactRoute.END,
        }

    async def tools(state):
        loop.calls.append("tools")
        return {"next_node": ReactRoute.OBSERVE}

    async def observe(state):
        loop.calls.append("observe")
        return {"next_node": ReactRoute.PREPARE_INPUT}

    monkeypatch.setattr(module, "_model_node", model)
    monkeypatch.setattr(module, "_tools_node", tools)
    monkeypatch.setattr(module, "_observe_node", observe)
    await module.ReactLikeWorkflow().run(loop.ops)
    # 终态由产出节点就地落定，loop 到 END 只停止驱动；每次模型请求前先经 prepare_input 并入 inbox。
    assert loop.calls == ["queue", "model", "tools", "observe", "queue", "model"]
    loop.model.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_resume_does_not_replay_old_tools(loop, monkeypatch):
    loop.run.execution_data = ConversationRunProgress(step_count=3, tool_error_count=2)

    async def model(state):
        assert state.step_count == 3
        from app.core.workflow.node_helper.common import _runtime_context

        assert _runtime_context().batch.calls == []
        return {"next_node": ReactRoute.END}

    monkeypatch.setattr(module, "_model_node", model)
    monkeypatch.setattr(
        module, "_tools_node", AsyncMock(side_effect=AssertionError("工具不应重放"))
    )
    await module.ReactLikeWorkflow().run(loop.ops)
    assert loop.progress[0].step_count == 3


@pytest.mark.asyncio
async def test_cancellation_before_model_does_not_consume_queue(loop):
    """取消时 prepare_input 不并入 inbox，由 model 节点入口落定取消，排队消息不进已取消的 Run。"""
    loop.ops.cancelled = True
    await module.ReactLikeWorkflow().run(loop.ops)
    assert loop.calls == ["cancel"]
    loop.model.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_suspend_ends_driving_without_another_model_request(loop, monkeypatch):
    """observe 落定挂起后返回 END：本次驱动结束，不再并入队列、不再请求模型。"""

    async def model(state):
        loop.calls.append("model")
        return {"next_node": ReactRoute.TOOLS}

    async def tools(state):
        loop.calls.append("tools")
        return {"next_node": ReactRoute.OBSERVE}

    async def observe(state):
        loop.calls.append("observe")
        return {"next_node": ReactRoute.END}

    monkeypatch.setattr(module, "_model_node", model)
    monkeypatch.setattr(module, "_tools_node", tools)
    monkeypatch.setattr(module, "_observe_node", observe)
    await module.ReactLikeWorkflow().run(loop.ops)
    assert loop.calls == ["queue", "model", "tools", "observe"]
    loop.model.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "invalid",
        "error",
        "late_cancel",
        "persist_error",
        "missing_context",
        "tools",
        "tools_empty",
        "truncated",
    ],
)
async def test_structured_output_updates_completed_run_result(loop, monkeypatch, outcome):
    """主线先完成；结构化支线成功只替换最终输出，失败（含结果写入失败）只记录失败原因。

    成功后必须立即结束：回归点是成功写入后又落到失败写入，把 end_reason 覆盖成失败。
    """
    from app.core.workflow.node_helper.common import _runtime_context
    loop.ops.agent.structured_output = SimpleNamespace(
        name="result", json_schema={"type": "object", "required": ["ok"]}
    )
    loop.ops.task = SimpleNamespace(id=2, workspace_id=1)
    timeline = []
    updates = []

    def finish(transition, **kwargs):
        assert transition is RunTransition.COMPLETE
        timeline.append(("complete", kwargs["final_output"]))
        return SimpleNamespace(status="completed")

    def update(**kwargs):
        timeline.append(("update", kwargs.get("final_output"), kwargs.get("end_reason")))
        updates.append(kwargs)
        if outcome == "persist_error" and kwargs.get("final_output") is not None:
            raise OSError("storage unavailable")
        return SimpleNamespace(status="completed")

    if outcome == "missing_context":

        def missing_context():
            raise RuntimeError("context unavailable")

        loop.context.load_message = missing_context

    loop.ops.run_state.transition = lambda _run_id, transition, **kwargs: finish(
        transition, **kwargs
    )
    loop.ops.run_state.update_completed_result = lambda _run_id, **kwargs: update(**kwargs)

    async def model(state):
        runtime = _runtime_context()
        # 主线先完成：落定 completed 后再走结构化支线。
        assert (
            runtime.run_state.transition(
                runtime.run.id,
                RunTransition.COMPLETE,
                usage_stats=runtime.usage_stats,
                final_output="自然回答",
            )
            is not None
        )
        return {"next_node": ReactRoute.STRUCTURED_OUTPUT}

    async def complete(messages, **kw):
        assert timeline == [("complete", "自然回答")]
        loop.calls.append("format")
        assert all(message.get("content") is not None for message in messages)
        assert all(
            not message.get("tool_calls") for message in messages if message["role"] == "assistant"
        )
        if outcome in {"tools", "tools_empty"}:
            return sdk_completion(
                '{"ok":true}' if outcome == "tools" else None,
                tool_calls=[
                    {
                        "id": "unexpected",
                        "type": "function",
                        "function": {"name": "write_file", "arguments": "{}"},
                    }
                ],
            )
        if outcome == "error":
            raise ConnectionError("provider unavailable")
        if outcome == "late_cancel":
            loop.ops.cancelled = True
        return sdk_completion(
            '{"ok":true}' if outcome != "invalid" else "invalid",
            finish_reason="length" if outcome == "truncated" else "stop",
        )

    loop.model.complete = complete
    monkeypatch.setattr(module, "_model_node", model)
    await module.ReactLikeWorkflow().run(loop.ops)
    assert loop.calls.count("format") == (
        0
        if outcome == "missing_context"
        else 4
        if outcome in {"invalid", "tools_empty"}
        else 1
    )
    # 正文合规即采纳：多余 tool_calls 与 length 截断都不丢弃可用结果。
    if outcome in {"success", "late_cancel", "tools", "truncated"}:
        assert len(updates) == 1
        assert updates[0]["final_output"] == '{"ok":true}'
        assert updates[0].get("end_reason") is None
    elif outcome == "persist_error":
        # 结构化结果没能落库：记一次失败原因，不保留成功写入的假象。
        assert [update.get("end_reason") for update in updates] == [
            None,
            "structured_output_failed",
        ]
    else:
        assert len(updates) == 1
        assert updates[0]["end_reason"] == "structured_output_failed"
        assert updates[0].get("final_output") is None
    assert timeline[0] == ("complete", "自然回答")
    loop.model.close.assert_awaited_once()
