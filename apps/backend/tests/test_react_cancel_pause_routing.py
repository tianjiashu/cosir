"""Regression tests for ReAct cancellation interrupts and checkpoint resumption."""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.core.workflows.react.edges import _after_observe, _after_tools, _should_continue
from app.core.workflows.react.state import ReactGraphState

model_node_module = importlib.import_module("app.core.workflows.nodes.model_node")


def _state(**overrides: Any) -> ReactGraphState:
    """Build the minimal valid graph state used by routing and interrupt tests."""

    values: dict[str, Any] = {
        "step_count": 1,
        "tool_error_count": 0,
        "requested_tool": False,
        "final_response": False,
        "terminal": False,
        "max_steps": 10,
        "final_text": "",
        "last_tool_results": {},
    }
    values.update(overrides)
    return ReactGraphState(**values)


def test_react_edges_route_only_from_current_graph_state() -> None:
    """The current graph has model/tools/observe edges; cancellation interrupts in model."""

    assert _should_continue(_state(requested_tool=True)) == "tools"
    assert _should_continue(_state(continue_model=True)) == "model"
    assert _should_continue(_state(final_response=True)) == END
    assert _should_continue(_state(terminal=True)) == END
    assert _should_continue(_state()) == END

    assert _after_tools(_state()) == "observe"
    assert _after_tools(_state(terminal=True)) == END
    assert _after_observe(_state()) == "model"
    assert _after_observe(_state(final_response=True)) == END


def test_model_node_interrupts_when_run_was_cancelled_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-request cancellation settles the Run and interrupts the model node."""

    current_run = SimpleNamespace(id=9, task_id=5)
    usage_stats = object()
    cancel_calls: list[dict[str, Any]] = []
    runtime_config = SimpleNamespace(
        operations=SimpleNamespace(
            get_current_run=lambda: current_run,
            is_current_run_cancelled=lambda: True,
            cancel_run_if_running=lambda **kwargs: cancel_calls.append(kwargs),
        ),
        run=current_run,
        model=object(),
        thinking_channel=None,
        usage_stats=usage_stats,
    )

    class _NodeInterrupted(Exception):
        def __init__(self, payload: dict[str, str]) -> None:
            self.payload = payload

    def _interrupt(payload: dict[str, str]) -> None:
        raise _NodeInterrupted(payload)

    monkeypatch.setattr(model_node_module, "_runtime_config", lambda: runtime_config)
    monkeypatch.setattr(model_node_module, "get_stream_writer", lambda: object())
    monkeypatch.setattr(model_node_module, "interrupt", _interrupt)

    with pytest.raises(_NodeInterrupted) as raised:
        asyncio.run(model_node_module._model_node(_state()))

    assert raised.value.payload == {"reason": "user_cancelled"}
    assert cancel_calls == [{"usage_stats": usage_stats, "final_output": "user_cancelled"}]


def test_langgraph_interrupt_checkpoint_can_resume_the_interrupted_node() -> None:
    """Cancellation checkpoints the model node; resume re-enters it and can finish."""

    cancellation = [True]
    calls: list[str] = []

    async def _model_node(_state: ReactGraphState) -> dict[str, bool]:
        calls.append("model")
        if cancellation[0]:
            interrupt({"reason": "user_cancelled"})
        return {"terminal": True}

    builder = StateGraph(ReactGraphState)
    builder.add_node("model", _model_node)
    builder.add_edge(START, "model")
    builder.add_edge("model", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "cancel-interrupt-resume"}}

    async def _run() -> None:
        await graph.ainvoke(_state(), config)
        snapshot = await graph.aget_state(config)
        assert snapshot.next == ("model",)
        assert any(task.interrupts for task in snapshot.tasks)

        cancellation[0] = False
        await graph.ainvoke(Command(resume={"action": "resume"}), config)

        snapshot = await graph.aget_state(config)
        assert snapshot.next == ()
        assert calls == ["model", "model"]

    asyncio.run(_run())


def test_finished_graph_has_no_resumable_next_node() -> None:
    """A completed graph has an empty next tuple and cannot be resumed."""

    async def _finish(_state: ReactGraphState) -> dict[str, bool]:
        return {"terminal": True}

    builder = StateGraph(ReactGraphState)
    builder.add_node("finish", _finish)
    builder.add_edge(START, "finish")
    builder.add_edge("finish", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "finished-graph"}}

    async def _run() -> None:
        await graph.ainvoke(_state(), config)
        snapshot = await graph.aget_state(config)
        assert snapshot.next == ()

    asyncio.run(_run())
