"""直接验证业务循环边界，不依赖图框架的节点重放。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.core.workflow.workflow as module
from app.core.workflow.execution_state.route import ReactRoute
from tests.sdk_fixtures import sdk_completion


@pytest.fixture
def loop(monkeypatch):
    progress = []
    calls = []
    context = SimpleNamespace(
        begin_run=lambda *args: None, consume_user_inputs=lambda: calls.append("queue")
    )
    model = SimpleNamespace(close=AsyncMock())
    profile = SimpleNamespace(max_steps=10, structured_output=None)
    run = SimpleNamespace(id=1, task_id=2, execution_data={}, usage=None)
    operations = SimpleNamespace(
        get_current_run=lambda: run,
        agent_profile=profile,
        task_tool_schemas=[],
        is_current_run_cancelled=lambda: False,
        save_execution=lambda value: progress.append(value),
        complete_run_if_running=lambda *a, **k: calls.append("complete"),
        cancel_run_if_running=lambda **k: calls.append("cancel"),
    )
    monkeypatch.setattr(
        module,
        "resolve_chat_model",
        lambda **kw: SimpleNamespace(model=model, supports_thinking=False),
    )
    monkeypatch.setattr(
        module, "get_tool_system", lambda: SimpleNamespace(user_input_decision_handlers=None)
    )
    monkeypatch.setattr(
        module.task_runtime_spaces,
        "get_or_create",
        lambda _: SimpleNamespace(get_context_manager=lambda **k: context),
    )
    monkeypatch.setattr(module, "log_tools_fingerprint", lambda *a, **k: None)
    return SimpleNamespace(run=run, ops=operations, progress=progress, calls=calls, model=model)


@pytest.mark.asyncio
async def test_queue_only_at_model_boundary_and_final_ends_run(loop, monkeypatch):
    count = 0

    async def model(state):
        nonlocal count
        count += 1
        loop.calls.append("model")
        return {
            "step_count": count,
            "next_node": ReactRoute.TOOLS if count == 1 else ReactRoute.END,
            "final_text": "done" if count == 2 else "",
        }

    async def tools(state):
        loop.calls.append("tools")
        return {"next_node": ReactRoute.OBSERVE}

    async def observe(state):
        loop.calls.append("observe")
        return {"next_node": ReactRoute.MODEL}

    monkeypatch.setattr(module, "_model_node", model)
    monkeypatch.setattr(module, "_tools_node", tools)
    monkeypatch.setattr(module, "_observe_node", observe)
    await module.ReactLikeWorkflow().run(loop.ops)
    assert loop.calls == ["queue", "model", "tools", "observe", "queue", "model", "complete"]
    loop.model.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_resume_does_not_replay_old_tools(loop, monkeypatch):
    loop.run.execution_data = {"step_count": 3, "tool_error_count": 2}

    async def model(state):
        assert state.step_count == 3
        assert state.tool_call_lifecycle is None
        return {"next_node": ReactRoute.END, "final_text": "resumed"}

    monkeypatch.setattr(module, "_model_node", model)
    monkeypatch.setattr(
        module, "_tools_node", AsyncMock(side_effect=AssertionError("工具不应重放"))
    )
    await module.ReactLikeWorkflow().run(loop.ops, execution_mode="resume")
    assert loop.calls == ["queue", "complete"]
    assert loop.progress[0]["step_count"] == 4


@pytest.mark.asyncio
async def test_cancellation_before_model_does_not_consume_queue(loop):
    loop.ops.is_current_run_cancelled = lambda: True
    await module.ReactLikeWorkflow().run(loop.ops)
    assert loop.calls == ["cancel"]
    loop.model.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_wait_returns_without_extra_model_or_queue(loop, monkeypatch):
    async def model(state):
        return {"next_node": ReactRoute.WAIT_USER}

    monkeypatch.setattr(module, "_model_node", model)
    monkeypatch.setattr(module, "suspend_for_input", lambda _: loop.calls.append("wait"))
    await module.ReactLikeWorkflow().run(loop.ops)
    assert loop.calls == ["queue", "wait"]
    loop.model.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["success", "invalid", "error", "cancel", "tools", "tools_empty", "truncated"]
)
async def test_structured_output_finishes_before_run_completion(loop, monkeypatch, outcome):
    """格式化期间仍属原 Run；仅完整校验后完成，队列不在格式化请求前消费。"""
    from app.core.workflow.nodes import structured_output_node as structured

    context = SimpleNamespace(load_message=lambda: [])
    monkeypatch.setattr(structured, "_runtime_context", lambda: context)
    monkeypatch.setattr(structured, "resolve_messages_for_model", lambda messages, **kw: messages)
    loop.ops.agent_profile.structured_output = SimpleNamespace(
        name="result", json_schema={"type": "object", "required": ["ok"]}
    )
    loop.ops.get_current_task = lambda: SimpleNamespace(workspace_id=1)
    outputs = []
    loop.ops.complete_run_if_running = lambda *a, **kw: outputs.append(kw["final_output"])

    async def model(state):
        return {"next_node": ReactRoute.STRUCTURED_OUTPUT, "final_text": "自然回答"}

    async def complete(messages, **kw):
        assert outputs == []
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
        if outcome == "cancel":
            loop.ops.is_current_run_cancelled = lambda: True
        return sdk_completion(
            '{"ok":true}' if outcome != "invalid" else "invalid",
            finish_reason="length" if outcome == "truncated" else "stop",
        )

    loop.model.complete = complete
    monkeypatch.setattr(module, "_model_node", model)
    if outcome == "error":
        with pytest.raises(ConnectionError):
            await module.ReactLikeWorkflow().run(loop.ops)
    else:
        await module.ReactLikeWorkflow().run(loop.ops)
    assert loop.calls.count("queue") == 1
    assert outputs == (
        {
            "success": ['{"ok":true}'],
            "invalid": ["自然回答"],
            "tools": ["自然回答"],
            "tools_empty": ["自然回答"],
            "truncated": ["自然回答"],
        }.get(outcome, [])
    )
    if outcome == "cancel":
        assert loop.calls[-1] == "cancel"
    loop.model.close.assert_awaited_once()
