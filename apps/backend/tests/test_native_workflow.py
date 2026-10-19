"""直接验证业务循环边界，不依赖图框架的节点重放。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.core.workflow.workflow as module
from app.core.workflow.execution_state.react_route import ReactRoute
from app.models.conversation_run_progress import ConversationRunProgress
from tests.sdk_fixtures import sdk_completion


@pytest.fixture
def loop(monkeypatch):
    progress = []
    calls = []
    context = SimpleNamespace(begin_run=lambda *args: None)
    monkeypatch.setattr(
        module,
        "TaskInputService",
        lambda: SimpleNamespace(consume_into_context=lambda *args: calls.append("queue")),
    )
    model = SimpleNamespace(close=AsyncMock())
    profile = SimpleNamespace(max_steps=10, structured_output=None)
    run = SimpleNamespace(id=1, task_id=2, execution_data=ConversationRunProgress(), usage=None)
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
    return SimpleNamespace(run=run, ops=operations, progress=progress, calls=calls, model=model)


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
        return {"next_node": ReactRoute.MODEL}

    monkeypatch.setattr(module, "_model_node", model)
    monkeypatch.setattr(module, "_tools_node", tools)
    monkeypatch.setattr(module, "_observe_node", observe)
    await module.ReactLikeWorkflow().run(loop.ops)
    # 终态由产出节点就地落定，loop 到 END 只停止驱动，不再二次落库。
    assert loop.calls == ["queue", "model", "tools", "observe", "queue", "model"]
    loop.model.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_resume_does_not_replay_old_tools(loop, monkeypatch):
    loop.run.execution_data = ConversationRunProgress(step_count=3, tool_error_count=2)

    async def model(state):
        assert state.step_count == 3
        assert state.tool_call_lifecycle is None
        return {"next_node": ReactRoute.END}

    monkeypatch.setattr(module, "_model_node", model)
    monkeypatch.setattr(
        module, "_tools_node", AsyncMock(side_effect=AssertionError("工具不应重放"))
    )
    await module.ReactLikeWorkflow().run(loop.ops, execution_mode="resume")
    assert loop.calls == ["queue"]
    assert loop.progress[0].step_count == 4


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
    """主线先完成；结构化支线只替换最终输出或失败原因，不重复迁移 Run。"""
    from app.core.workflow.node_helper.common import _runtime_context
    from app.core.workflow.nodes import structured_output_node as structured

    monkeypatch.setattr(structured, "resolve_messages_for_model", lambda messages, **kw: messages)
    loop.ops.agent_profile.structured_output = SimpleNamespace(
        name="result", json_schema={"type": "object", "required": ["ok"]}
    )
    loop.ops.get_current_task = lambda: SimpleNamespace(workspace_id=1)
    timeline = []
    updates = []

    def finish(*args, **kwargs):
        timeline.append(("complete", kwargs["final_output"]))
        return SimpleNamespace(status="completed")

    def update(**kwargs):
        timeline.append(("update", kwargs.get("final_output"), kwargs.get("end_reason")))
        updates.append(kwargs)
        if outcome == "persist_error":
            raise OSError("storage unavailable")
        return SimpleNamespace(status="completed")

    loop.ops.complete_run_if_running = finish
    loop.ops.update_completed_result = update

    async def model(state):
        runtime = _runtime_context()
        runtime.structured_output_messages = (
            None if outcome == "missing_context" else [{"role": "assistant", "content": "自然回答"}]
        )
        # 主线先完成：落定 completed 后再走结构化支线（未取消时直接 complete_run_if_running）。
        assert (
            runtime.operations.complete_run_if_running(
                runtime.usage_stats, final_output="自然回答"
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
            loop.ops.is_current_run_cancelled = lambda: True
        return sdk_completion(
            '{"ok":true}' if outcome != "invalid" else "invalid",
            finish_reason="length" if outcome == "truncated" else "stop",
        )

    loop.model.complete = complete
    monkeypatch.setattr(module, "_model_node", model)
    await module.ReactLikeWorkflow().run(loop.ops)
    assert loop.calls.count("queue") == 1
    assert loop.calls.count("format") == (
        0
        if outcome == "missing_context"
        else 4
        if outcome in {"invalid", "tools", "tools_empty", "truncated"}
        else 1
    )
    assert len(updates) == 1
    if outcome in {"success", "late_cancel", "persist_error"}:
        assert updates[0]["final_output"] == '{"ok":true}'
        assert updates[0].get("end_reason") is None
    else:
        assert updates[0]["end_reason"] == "structured_output_failed"
        assert updates[0].get("final_output") is None
    assert timeline[0] == ("complete", "自然回答")
    loop.model.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision_kind", ["approve", "reject"])
async def test_wait_resume_roundtrip_only_reopens_decided_call(loop, monkeypatch, decision_kind):
    """持久等待数据恢复为可执行状态；已完成工具不重放，决定消费后不再留恢复入口。"""
    from app.core.tools.schemas.tool_call_lifecycle_state import (
        ToolCallLifecycleRecord,
        ToolCallLifecycleState,
    )
    from app.core.tools.schemas.user_decision import UserDecision, UserDecisionKind
    from app.models.conversation_run_progress import RunWaitingState, ToolBatchSnapshot

    decision = UserDecision(kind=UserDecisionKind(decision_kind), request_id="request")
    progress = ConversationRunProgress(
        step_count=4,
        waiting=RunWaitingState(
            instruction="continue",
            last_tool_results=ToolBatchSnapshot(
                instruction="continue",
                observations=[
                    {
                        "tool_call_id": "waiting",
                        "tool_name": "review",
                        "status": "success",
                        "content": "waiting for approval",
                        "user_input_request": {
                            "request_id": "request",
                            "kind": "review",
                            "prompt": "allow?",
                            "decisions": ["approve", "reject"],
                        },
                    }
                ],
            ),
            tool_call_lifecycle=ToolCallLifecycleState(
                allows_tools=("review",),
                valid_calls={
                    "completed": ToolCallLifecycleRecord(
                        tool_call_id="completed",
                        tool_name="review",
                        status="completed",
                    ),
                    "waiting": ToolCallLifecycleRecord(
                        tool_call_id="waiting",
                        tool_name="review",
                        status="running",
                    ),
                },
            ),
        ),
        decision=decision,
    )
    loop.run.execution_data = ConversationRunProgress.model_validate_json(
        progress.model_dump_json()
    )
    monkeypatch.setattr(
        module,
        "get_tool_system",
        lambda: SimpleNamespace(
            user_input_decision_handlers=SimpleNamespace(
                handle_rejection=lambda request, answer: loop.calls.append("reject"),
            ),
        ),
    )

    async def tools(state):
        assert decision_kind == "approve"
        pending = [
            call for call in state.tool_call_lifecycle.valid_tools if call.status == "pending"
        ]
        assert [call.tool_call_id for call in pending] == ["waiting"]
        assert pending[0].user_decision == decision
        loop.calls.append("approved_tool")
        return {"next_node": ReactRoute.OBSERVE}

    async def observe(state):
        assert state.tool_call_lifecycle.valid_calls["completed"].status == "completed"
        assert state.last_tool_results["observations"][0].get("user_input_request") is None
        loop.calls.append("observe")
        return {"next_node": ReactRoute.END}

    monkeypatch.setattr(module, "_tools_node", tools)
    monkeypatch.setattr(module, "_observe_node", observe)
    monkeypatch.setattr(
        module, "_model_node", AsyncMock(side_effect=AssertionError("不应先请求模型"))
    )
    await module.ReactLikeWorkflow().run(loop.ops, execution_mode="resume_with_input")
    assert loop.calls == [
        "approved_tool" if decision_kind == "approve" else "reject",
        "observe",
    ]
    assert all(saved.waiting is None and saved.decision is None for saved in loop.progress)
