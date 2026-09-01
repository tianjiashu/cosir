"""Tests for max-step termination through the canonical run mutation boundary."""

from app.core.workflows.nodes.finalize_max_steps import (
    _MAX_STEPS_FINAL_TEXT,
    _finalize_max_steps,
)
from app.core.workflows.react.state import ReactGraphState
from app.models.turn_usage_stats import TurnUsageStats


def _make_state(**overrides) -> ReactGraphState:
    """构造一个合法的 ``ReactGraphState`` 实例。

    参数:
        overrides: 需要覆盖的字段值。
    返回:
        字段齐备的终态前 state。
    """
    base = {
        "repair_requested": False,
        "step_count": 3,
        "tool_error_count": 0,
        "requested_tool": False,
        "final_response": False,
        "terminal": False,
        "pending_tool_calls": [],
        "max_steps": 5,
        "final_text": "",
        "last_tool_results": [],
    }
    base.update(overrides)
    return ReactGraphState(**base)


def _make_runtime_config(monkeypatch, *, fail_result) -> dict:
    """Inject a runtime stub whose failure mutation returns ``fail_result``."""

    class _Ops:
        def fail_turn_if_running(self, turn_id: str, end_reason=None):
            return fail_result

    usage = TurnUsageStats()
    usage.input_tokens = 7
    usage.output_tokens = 9

    rc = type(
        "RuntimeConfigStub",
        (),
        {
            "operations": _Ops(),
            "turn": type("TurnStub", (), {"turn_id": "turn-fixed"})(),
            "usage_stats": usage,
            "langfuse_trace_id": "trace-xyz",
        },
    )()

    monkeypatch.setattr(
        "app.core.workflows.nodes.finalize_max_steps._runtime_config",
        lambda: rc,
    )
    return {"turn_id": "turn-fixed"}


async def test_finalize_max_steps_sets_end_reason_in_run_failed(monkeypatch) -> None:
    """Canonical mutation receives max-step reason and state gets stable text."""
    _make_runtime_config(monkeypatch, fail_result=object())
    state = _make_state(step_count=3, continuation_error_data=None)

    patch = await _finalize_max_steps(state, step_count=4)

    assert patch["final_text"] == _MAX_STEPS_FINAL_TEXT
    assert patch["step_count"] == 4
    assert patch["terminal"] is True


async def test_finalize_max_steps_uses_state_step_count_when_not_given(monkeypatch) -> None:
    """``step_count`` 缺省时回退 ``state.step_count`` 生成 step_id。"""
    _make_runtime_config(monkeypatch, fail_result=object())
    state = _make_state(step_count=2)

    await _finalize_max_steps(state)


async def test_finalize_max_steps_race_no_event_when_turn_not_running(monkeypatch) -> None:
    """turn 已非 running（race 失败）时跳过失败事件，避免重复 emit。"""
    _make_runtime_config(monkeypatch, fail_result=None)
    state = _make_state(step_count=3)

    patch = await _finalize_max_steps(state, step_count=4)

    assert patch["final_text"] == _MAX_STEPS_FINAL_TEXT
    assert patch["terminal"] is True


async def test_finalize_max_steps_preserves_continuation_error_data(monkeypatch) -> None:
    """终态 ``continuation_error_data`` 应并入事件 data 并写入默认 final_text。"""
    _make_runtime_config(monkeypatch, fail_result=object())
    state = _make_state(
        step_count=5,
        continuation_error_data={"error_kind": "max_steps", "detail": "limit"},
    )

    patch = await _finalize_max_steps(state, step_count=6)

    assert patch["final_text"] == _MAX_STEPS_FINAL_TEXT
