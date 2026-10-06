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

from app.core.workflows.react.edges import _route_target
from app.core.workflows.react.worflow_state.route import ReactRoute
from app.core.workflows.react.worflow_state.state import ReactGraphState

model_node_module = importlib.import_module("app.core.workflows.react.nodes.model_node")


def _state(**overrides: Any) -> ReactGraphState:
    """Build the minimal valid graph state used by routing and interrupt tests."""

    values: dict[str, Any] = {
        "step_count": 1,
        "tool_error_count": 0,
        "next_node": ReactRoute.MODEL,
        "max_steps": 10,
        "final_text": "",
        "last_tool_results": {},
    }
    values.update(overrides)
    return ReactGraphState(**values)


def test_react_edges_route_only_from_current_graph_state() -> None:
    """The current graph has model/tools/observe edges; cancellation interrupts in model."""

    assert _route_target(_state(next_node=ReactRoute.TOOLS)) == "tools"
    assert _route_target(_state(next_node=ReactRoute.MODEL)) == "model"
    assert _route_target(_state(next_node=ReactRoute.STRUCTURED_OUTPUT)) == "structured_output"
    assert _route_target(_state(next_node=ReactRoute.AGENT_TEAM_WAIT)) == "agent_team_wait"
    assert _route_target(_state(next_node=ReactRoute.END)) == END


def test_model_node_interrupts_when_run_was_cancelled_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-request cancellation settles the Run and interrupts the model node."""

    current_run = SimpleNamespace(id=9, task_id=5)
    usage_stats = object()
    cancel_calls: list[dict[str, Any]] = []
    runtime_config = SimpleNamespace(
        operations=SimpleNamespace(
            # model 节点在发起请求前先按 task 维度取延迟系统消息队列，故桩需暴露 get_current_task。
            get_current_task=lambda: SimpleNamespace(id=current_run.task_id),
            get_current_run=lambda: current_run,
            is_current_run_cancelled=lambda: True,
            cancel_run_if_running=lambda **kwargs: cancel_calls.append(kwargs),
        ),
        run=current_run,
        model=object(),
        workspace_id=1,
        thinking_channel="",
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

    async def _model_node(_state: ReactGraphState) -> dict[str, object]:
        calls.append("model")
        if cancellation[0]:
            interrupt({"reason": "user_cancelled"})
        return {"next_node": ReactRoute.END}

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

    async def _finish(_state: ReactGraphState) -> dict[str, object]:
        return {"next_node": ReactRoute.END}

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
