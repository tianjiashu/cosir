"""等待节点的请求派生、持久快照、决定校验与批准路由。"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.tools.schemas.user_decision import (
    UserDecision,
    UserDecisionKind,
)
from app.core.workflow.execution_state.route import ReactRoute
from app.core.workflow.execution_state.state import ReactExecutionState
from app.core.workflow.node_helper import (
    ToolCallLifecycleManager,
    ToolCallLifecycleRecord,
)

wait_node_module = importlib.import_module("app.core.workflow.nodes.wait_user_node")
lifecycle_module = importlib.import_module("app.core.workflow.node_helper.tool_call_lifecycle")

_TOOL_NAME = "agent_team"

# 「未指定」哨兵：让 ``lifecycle=None`` 表达「显式缺失生命周期」而不触发默认夹具。
_UNSET: Any = object()


def _request(
    *,
    request_id: str = "12",
    decisions: tuple[str, ...] = ("approve", "reject"),
) -> dict[str, Any]:
    """构造待决请求的投影形状（与 ``dataclasses.asdict(UserInputRequest)`` 一致）。"""

    return {
        "kind": "agent_team_review",
        "request_id": request_id,
        "prompt": "确认执行方案",
        "decisions": list(decisions),
        "draft_schema": "agent-team-review-v1",
        "draft": {"goal": "交付报告"},
    }


def _observation(
    tool_call_id: str = "call-a",
    *,
    request_id: str = "12",
    request: object = _UNSET,
    decisions: tuple[str, ...] = ("approve", "reject"),
) -> dict[str, Any]:
    """构造一条工具观察摘要；``request=None`` 表示该调用没有待决声明（已作答或普通结果）。"""

    return {
        "tool_name": _TOOL_NAME,
        "status": "success",
        "content": '{"status": "pending"}',
        "error": "",
        "reason": "",
        "retryable": False,
        "tool_call_id": tool_call_id,
        "display_data": {"kind": "agent-team-approval", "status": "pending"},
        "user_input_request": (
            _request(request_id=request_id, decisions=decisions) if request is _UNSET else request
        ),
    }


def _lifecycle(
    *call_ids: str,
    allows: tuple[str, ...] = (_TOOL_NAME,),
    blocked: tuple[str, ...] = (),
) -> ToolCallLifecycleManager:
    """构造已起跑调用的生命周期快照：``valid_calls`` 为 running，``blocked_calls`` 为未放行。"""

    return ToolCallLifecycleManager(
        allows_tools=allows,
        valid_calls={
            call_id: ToolCallLifecycleRecord(
                tool_call_id=call_id, tool_name=_TOOL_NAME, status="running"
            )
            for call_id in call_ids
        },
        blocked_calls={
            call_id: ToolCallLifecycleRecord(
                tool_call_id=call_id,
                tool_name="write_file",
                status="pending",
                part_projected=False,
            )
            for call_id in blocked
        },
    )


def _state(
    observations: list[dict[str, Any]],
    *,
    lifecycle: Any = _UNSET,
) -> ReactExecutionState:
    """构造等待节点所需的最小 graph state。"""

    return ReactExecutionState(
        step_count=1,
        tool_error_count=0,
        next_node=ReactRoute.OBSERVE,
        last_tool_results={"instruction": "调用 Team", "observations": observations},
        tool_call_lifecycle=(
            _lifecycle("call-a", "call-b", "call-c") if lifecycle is _UNSET else lifecycle
        ),
    )


@pytest.fixture
def runtime(monkeypatch):
    events = []
    writes = []
    rejections = []
    transitions = []
    runtime = SimpleNamespace(
        run=SimpleNamespace(id=99),
        usage_stats=None,
        operations=SimpleNamespace(
            get_current_task=lambda: SimpleNamespace(id=42),
            mark_waiting_for_input_if_running=lambda *a, **k: transitions.append(k),
        ),
        user_input_decision_handlers=SimpleNamespace(
            handle_rejection=lambda req, decision: rejections.append(decision)
        ),
    )
    monkeypatch.setattr(wait_node_module, "_runtime_context", lambda: runtime)
    monkeypatch.setattr(lifecycle_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(
        lifecycle_module,
        "_message_context",
        lambda: SimpleNamespace(add_message=lambda *a, **k: writes.append(k)),
    )
    return SimpleNamespace(
        events=events, writes=writes, rejections=rejections, transitions=transitions
    )


def test_wait_persists_request_and_execution_with_transition(runtime):
    state = _state([_observation()], lifecycle=_lifecycle("call-a"))
    wait_node_module.suspend_for_input(state)
    saved = runtime.transitions[0]["execution_data"]
    assert (
        saved.waiting.last_tool_results.observations[0]["user_input_request"]["request_id"] == "12"
    )
    assert runtime.writes[0]["metadata"].status == "running"
    assert len(runtime.events) == 1


def test_approval_only_reopens_requested_call(runtime):
    state = _state([_observation()], lifecycle=_lifecycle("call-a", "call-b"))
    patch = wait_node_module.wait_user_node(
        state, UserDecision(request_id="12", kind=UserDecisionKind.APPROVE)
    )
    assert patch["next_node"] == ReactRoute.TOOLS
    assert patch["tool_call_lifecycle"].valid_calls["call-a"].status == "pending"
    assert patch["tool_call_lifecycle"].valid_calls["call-b"].status == "running"
    assert patch["last_tool_results"]["observations"][0]["user_input_request"] is None
    assert runtime.events == []
    assert runtime.transitions == []


def test_rejection_closes_request_and_routes_to_observe(runtime):
    state = _state([_observation()], lifecycle=_lifecycle("call-a"))
    patch = wait_node_module.wait_user_node(
        state,
        UserDecision(request_id="12", kind=UserDecisionKind.REJECT, data={"feedback": "调整"}),
    )
    assert patch["next_node"] == ReactRoute.OBSERVE
    assert patch["last_tool_results"]["observations"][0]["status"] == "cancelled"
    assert len(runtime.rejections) == 1


@pytest.mark.parametrize(
    "decision",
    [
        UserDecision(request_id="stale", kind=UserDecisionKind.APPROVE),
        UserDecision(request_id="12", kind=UserDecisionKind.ABORT),
    ],
)
def test_invalid_decision_cannot_change_call(runtime, decision):
    state = _state([_observation()], lifecycle=_lifecycle("call-a"))
    with pytest.raises(ValueError):
        wait_node_module.wait_user_node(state, decision)
    assert state.tool_call_lifecycle.valid_calls["call-a"].status == "running"
    assert runtime.events == []


def test_multiple_pending_requests_are_rejected(runtime):
    state = _state([_observation(), _observation("call-b", request_id="13")])
    with pytest.raises(ValueError):
        wait_node_module.wait_user_node(
            state, UserDecision(request_id="12", kind=UserDecisionKind.APPROVE)
        )
