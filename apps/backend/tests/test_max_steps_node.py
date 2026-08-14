"""``_max_steps_node`` 兜底终态测试（invalid_tool_calls 自愈 Task 3 测试点 10）。

覆盖 ``continuation_error_data`` 并入 ``RUN_FAILED`` data 的契约（含修复回流耗尽场景），
以及 usage 透传与 race-lost 分支。错误分类固定为 ``max_steps_reached``（全库无 ``parse_invalid``）。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from app.core.workflows.nodes import max_steps_node
from app.core.workflows.react.runtime_config import RuntimeConfig
from app.core.workflows.react.state import ReactGraphState
from app.models.enums.event_type import EventType
from app.models.turn_usage_stats import TurnUsageStats


def _make_runtime_config() -> RuntimeConfig:
    """构造 max_steps 节点测试用运行时配置。

    参数:
        无。

    返回:
        带 mock operations / turn / model 的 ``RuntimeConfig``。

    异常:
        无。

    副作用:
        无。
    """

    operations = MagicMock()
    operations.fail_turn_if_running.return_value = MagicMock()
    turn = MagicMock()
    turn.turn_id = "turn_test"
    return RuntimeConfig(
        operations=operations,
        turn=turn,
        model=MagicMock(),
        usage_stats=TurnUsageStats(),
        langfuse_trace_id="trace_test",
    )


def _make_state(continuation_error_data: Any = None) -> ReactGraphState:
    """构造「已请求继续但达步数上限」的 graph state。

    参数:
        continuation_error_data: 终态携带的排查明细（``None`` 表示无明细）。

    返回:
        字段完整的 ``ReactGraphState``（``repair_requested`` 为 str 口径）。

    异常:
        无。

    副作用:
        无。
    """

    return ReactGraphState(
        step_count=3,
        tool_error_count=0,
        requested_tool=False,
        repair_requested="true",
        final_response=False,
        terminal=False,
        pending_tool_calls=[],
        max_steps=3,
        final_text="",
        last_tool_results=[],
        continuation_error_data=continuation_error_data,
    )


def _install(monkeypatch, rc: RuntimeConfig) -> list[tuple[EventType, object]]:
    """替换节点运行时依赖并捕获事件。

    参数:
        monkeypatch: pytest 注入的 monkeypatch fixture。
        rc: 测试用运行时配置。

    返回:
        事件捕获列表。

    异常:
        无。

    副作用:
        替换 ``max_steps_node`` 的 ``_runtime_config`` 与 ``write_event``。
    """

    events: list[tuple[EventType, object]] = []
    monkeypatch.setattr(max_steps_node, "_runtime_config", lambda: rc)
    monkeypatch.setattr(
        max_steps_node,
        "write_event",
        lambda event_type, payload: events.append((event_type, payload)),
    )
    return events


async def test_continuation_error_data_merged(monkeypatch) -> None:
    """测试目的：修复回流耗尽时 ``continuation_error_data`` 原样并入 RUN_FAILED 的 data
    （max_steps_reached 路径）。

    可能发现的缺陷：明细被丢弃 → 无法从终态事件区分「步数耗尽」是普通循环还是
    invalid_tool_call 修复死循环（plan 修复点 #7 / 测试点 10）。
    """

    rc = _make_runtime_config()
    events = _install(monkeypatch, rc)
    state = _make_state({"error_kind": "invalid_tool_call_repair", "invalid_count": 2})

    result = await max_steps_node._max_steps_node(state)

    rc.operations.fail_turn_if_running.assert_called_once_with(
        "turn_test", end_reason="max_steps_reached"
    )
    failed = [payload for event_type, payload in events if event_type == EventType.RUN_FAILED]
    assert failed
    payload = failed[0]
    # 分类必须是 max_steps_reached（不是被编造的 parse_invalid）
    assert payload.error == "max_steps_reached"
    assert payload.data == {"error_kind": "invalid_tool_call_repair", "invalid_count": 2}
    # 终态 patch 契约：str 口径 + 清空 continuation
    assert result["terminal"] is True
    assert result["repair_requested"] == "false"
    assert result["requested_tool"] is False
    assert result["pending_tool_calls"] == []
    assert result["continuation_error_data"] is None


async def test_repair_continuation_error_data_merges_into_run_failed(monkeypatch) -> None:
    """测试目的：同 ``test_continuation_error_data_merged`` 的回归别名，确保明细并入 data。

    可能发现的缺陷：明细被丢弃 → 无法从终态事件区分修复死循环与普通循环。
    """

    rc = _make_runtime_config()
    events = _install(monkeypatch, rc)
    state = _make_state({"error_kind": "invalid_tool_call_repair", "invalid_count": 2})

    result = await max_steps_node._max_steps_node(state)

    failed = [payload for event_type, payload in events if event_type == EventType.RUN_FAILED]
    assert failed
    assert failed[0].data == {"error_kind": "invalid_tool_call_repair", "invalid_count": 2}
    assert result["continuation_error_data"] is None


async def test_no_continuation_error_data_yields_none_data(monkeypatch) -> None:
    """测试目的：无明细时 RUN_FAILED 的 ``data`` 为 ``None``（不写空 dict 噪声）。

    可能发现的缺陷：写入 ``{}`` 使前端误判「有明细但为空」；或对 ``None`` 直接
    ``dict(None)`` 抛 TypeError。
    """

    rc = _make_runtime_config()
    events = _install(monkeypatch, rc)

    result = await max_steps_node._max_steps_node(_make_state(None))

    failed = [payload for event_type, payload in events if event_type == EventType.RUN_FAILED]
    assert failed
    assert failed[0].error == "max_steps_reached"
    assert failed[0].data is None
    assert result["terminal"] is True


async def test_continuation_error_data_is_copied_not_mutated(monkeypatch) -> None:
    """测试目的：节点不得就地修改 state 里的 ``continuation_error_data`` 字典。

    可能发现的缺陷：直接复用同一 dict 引用并注入 step 字段 → 污染 checkpoint 中的
    原始明细，重放时数据漂移。
    """

    rc = _make_runtime_config()
    events = _install(monkeypatch, rc)
    original = {"error_kind": "invalid_tool_call_repair", "invalid_count": 1}
    state = _make_state(original)

    await max_steps_node._max_steps_node(state)

    assert original == {"error_kind": "invalid_tool_call_repair", "invalid_count": 1}
    failed = [payload for event_type, payload in events if event_type == EventType.RUN_FAILED]
    assert failed[0].data is not original


async def test_run_failed_carries_token_usage(monkeypatch) -> None:
    """测试目的：max_steps 失败事件携带本 turn 已累计的 token 字段。

    可能发现的缺陷：漏展开 usage 六字段 → 前端 token 统计在兜底终态归零。
    """

    rc = _make_runtime_config()
    rc.usage_stats.add_message_usage(
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "prompt_cache_hit_tokens": 3,
            "prompt_cache_miss_tokens": 7,
            "reasoning_tokens": 1,
        }
    )
    events = _install(monkeypatch, rc)

    await max_steps_node._max_steps_node(_make_state({"error_kind": "invalid_tool_call_repair"}))

    failed = [payload for event_type, payload in events if event_type == EventType.RUN_FAILED]
    assert failed
    payload = failed[0]
    assert payload.input_tokens == 10
    assert payload.output_tokens == 5
    assert payload.total_tokens == 15
    assert payload.cache_hit_tokens == 3
    assert payload.cache_miss_tokens == 7
    assert payload.reasoning_tokens == 1


async def test_terminal_race_lost_skips_event_but_still_terminal(monkeypatch) -> None:
    """测试目的：turn 已非 running（竞态失败）时不重复发事件，但仍返回终态。

    可能发现的缺陷：竞态分支漏 return → 同一 turn 发出重复 RUN_FAILED；或返回
    非终态使 graph 继续空转。
    """

    rc = _make_runtime_config()
    rc.operations.fail_turn_if_running.return_value = None
    events = _install(monkeypatch, rc)

    result = await max_steps_node._max_steps_node(
        _make_state({"error_kind": "invalid_tool_call_repair"})
    )

    assert [event_type for event_type, _payload in events] == []
    assert result["terminal"] is True
    assert result["continuation_error_data"] is None


async def test_terminal_patch_is_accepted_by_state_schema(monkeypatch) -> None:
    """测试目的：终态 patch 可被 ``ReactGraphState`` 直接合并（schema 字段完整）。

    可能发现的缺陷：``continuation_error_data`` 未在 state 声明 → strict 校验下
    ``max_steps_node`` 读写该字段即崩溃（plan 修复点 #7）。
    """

    rc = _make_runtime_config()
    _install(monkeypatch, rc)
    state = _make_state({"error_kind": "invalid_tool_call_repair", "invalid_count": 1})

    patch = await max_steps_node._max_steps_node(state)
    merged = state.model_copy(update=patch)

    assert merged.terminal is True
    assert merged.repair_requested == "false"
    assert merged.continuation_error_data is None
