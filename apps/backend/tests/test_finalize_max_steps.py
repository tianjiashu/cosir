"""``finalize_max_steps._finalize_max_steps`` 步数耗尽收口的单元测试。

验证「max_steps 失败默认文本可达」修复的**源头**：``model_node`` 调用
``_finalize_max_steps`` 落定步数耗尽失败时，发出的 ``RUN_FAILED`` 事件必须携带
``end_reason="max_steps_reached"``（供前端 ``StatusBadge`` 与父 Agent
``ChildAgentRunner`` 按枚举分类渲染可读说明），并写入默认 ``final_text``。
同时验证 turn 非 running（race 失败）时**不发** ``RUN_FAILED`` 事件（避免重复事件）。

``_finalize_max_steps`` 依赖 ``_runtime_config()``（取 LangGraph 运行上下文）与
``write_event``（写自定义事件流），此处以 monkeypatch 注入桩验证其行为契约。
"""

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
    """构造并注入 ``_runtime_config`` 桩，返回捕获事件的容器。

    参数:
        monkeypatch: pytest monkeypatch 夹具。
        fail_result: ``fail_turn_if_running`` 的返回值（TurnRecord 桩或 None）。
    返回:
        ``{"captured": captured_events, "turn_id": turn_id}``。
    """
    captured: list = []

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
    monkeypatch.setattr(
        "app.core.workflows.nodes.finalize_max_steps.write_event",
        lambda event_type, payload: captured.append((event_type, payload)),
    )
    return {"captured": captured, "turn_id": "turn-fixed"}


async def test_finalize_max_steps_sets_end_reason_in_run_failed(monkeypatch) -> None:
    """正常收口时 RUN_FAILED 事件必须携带 end_reason=max_steps_reached 与默认 final_text。"""
    ctx = _make_runtime_config(monkeypatch, fail_result=object())
    state = _make_state(step_count=3, continuation_error_data=None)

    patch = await _finalize_max_steps(state, step_count=4)

    # 事件确已发出且仅一条 RUN_FAILED。
    assert len(ctx["captured"]) == 1
    event_type, payload = ctx["captured"][0]
    from app.models.enums.event_type import EventType

    assert event_type == EventType.RUN_FAILED
    assert payload.end_reason == "max_steps_reached"
    assert payload.error == "max_steps_reached"
    assert payload.step_id == "step-4"
    # 默认失败文本随事件 data 下发，供父 Agent / 用户感知停止原因。
    assert payload.data is not None
    assert payload.data["final_text"] == _MAX_STEPS_FINAL_TEXT
    # 终态 patch 含默认 final_text。
    assert patch["final_text"] == _MAX_STEPS_FINAL_TEXT
    assert patch["step_count"] == 4
    assert patch["terminal"] is True


async def test_finalize_max_steps_uses_state_step_count_when_not_given(monkeypatch) -> None:
    """``step_count`` 缺省时回退 ``state.step_count`` 生成 step_id。"""
    ctx = _make_runtime_config(monkeypatch, fail_result=object())
    state = _make_state(step_count=2)

    await _finalize_max_steps(state)

    assert len(ctx["captured"]) == 1
    _, payload = ctx["captured"][0]
    assert payload.step_id == "step-2"


async def test_finalize_max_steps_race_no_event_when_turn_not_running(monkeypatch) -> None:
    """turn 已非 running（race 失败）时跳过失败事件，避免重复 emit。"""
    ctx = _make_runtime_config(monkeypatch, fail_result=None)
    state = _make_state(step_count=3)

    patch = await _finalize_max_steps(state, step_count=4)

    assert len(ctx["captured"]) == 0
    # 仍返回终态 patch，但带默认 final_text。
    assert patch["final_text"] == _MAX_STEPS_FINAL_TEXT
    assert patch["terminal"] is True


async def test_finalize_max_steps_preserves_continuation_error_data(monkeypatch) -> None:
    """终态 ``continuation_error_data`` 应并入事件 data 并写入默认 final_text。"""
    ctx = _make_runtime_config(monkeypatch, fail_result=object())
    state = _make_state(
        step_count=5,
        continuation_error_data={"error_kind": "max_steps", "detail": "limit"},
    )

    await _finalize_max_steps(state, step_count=6)

    assert len(ctx["captured"]) == 1
    _, payload = ctx["captured"][0]
    assert payload.data["error_kind"] == "max_steps"
    assert payload.data["detail"] == "limit"
    assert payload.data["final_text"] == _MAX_STEPS_FINAL_TEXT
