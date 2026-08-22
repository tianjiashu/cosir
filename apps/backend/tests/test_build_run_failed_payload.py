"""``build_run_failed_payload`` 统一失败事件构造的单元测试。

验证「max_steps 失败默认文本可达」修复的核心不变量：收口函数 ``build_run_failed_payload``
新增的 ``end_reason`` 参数必须原样透传进 ``RunFailedPayload``，供前端 ``StatusBadge`` 与
父 Agent（``ChildAgentRunner``）按枚举分类渲染可读说明；缺省时为 ``None`` 不写占位串。
"""

from app.core.workflows.nodes.helper.common import build_run_failed_payload
from app.models.turn_usage_stats import TurnUsageStats


def _make_usage(*, input_tokens: int = 0) -> TurnUsageStats:
    """构造携带指定输入 token 的 ``TurnUsageStats``。

    参数:
        input_tokens: 输入 token 数，用于验证六 token 字段透传。
    返回:
        已设置输入 token 的统计累加器。
    """
    usage = TurnUsageStats()
    usage.input_tokens = input_tokens
    return usage


def test_build_run_failed_payload_passes_end_reason() -> None:
    """``end_reason`` 显式传入时必须原样透传进 payload。"""
    payload = build_run_failed_payload(
        "step-3",
        "max_steps_reached",
        usage=_make_usage(),
        langfuse_trace_id="trace-abc",
        end_reason="max_steps_reached",
    )
    assert payload.end_reason == "max_steps_reached"
    assert payload.error == "max_steps_reached"
    assert payload.step_id == "step-3"
    assert payload.status == "failed"


def test_build_run_failed_payload_default_end_reason_is_none() -> None:
    """``end_reason`` 缺省（如 invalid_model_output 等非语义化分支）时为 ``None`` 不写占位串。"""
    payload = build_run_failed_payload(
        "step-2",
        "invalid_model_output",
        usage=_make_usage(),
        langfuse_trace_id=None,
    )
    assert payload.end_reason is None
    assert payload.error == "invalid_model_output"


def test_build_run_failed_payload_keeps_usage_and_data() -> None:
    """``end_reason`` 新增不破坏既有 usage 六字段与 data 透传。"""
    payload = build_run_failed_payload(
        "step-1",
        "max_steps_reached",
        usage=_make_usage(input_tokens=42),
        langfuse_trace_id="trace-x",
        data={"final_text": "stop"},
        end_reason="max_steps_reached",
    )
    assert payload.input_tokens == 42
    assert payload.data == {"final_text": "stop"}
    assert payload.end_reason == "max_steps_reached"


def test_build_run_failed_payload_custom_status_and_data_none() -> None:
    """``status`` 可显式非默认、``data`` 缺省为 None 时不写占位串（不变量保持）。"""
    payload = build_run_failed_payload(
        "step-9",
        "invalid_tool_call",
        usage=_make_usage(),
        langfuse_trace_id=None,
        status="failed",
    )
    assert payload.status == "failed"
    assert payload.data is None
    assert payload.end_reason is None
    assert payload.step_id == "step-9"


def test_build_run_failed_payload_full_six_token_flat_expansion() -> None:
    """六 token 扁平字段应逐字段透传，不遗漏 cache/reasoning 等次要字段。"""
    usage = TurnUsageStats()
    usage.input_tokens = 1
    usage.output_tokens = 2
    usage.total_tokens = 3
    usage.cache_hit_tokens = 4
    usage.cache_miss_tokens = 5
    usage.reasoning_tokens = 6
    payload = build_run_failed_payload(
        "step-1",
        "err",
        usage=usage,
        langfuse_trace_id="trace-1",
        end_reason="max_steps_reached",
    )
    assert payload.input_tokens == 1
    assert payload.output_tokens == 2
    assert payload.total_tokens == 3
    assert payload.cache_hit_tokens == 4
    assert payload.cache_miss_tokens == 5
    assert payload.reasoning_tokens == 6
    assert payload.langfuse_trace_id == "trace-1"
