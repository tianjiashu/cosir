"""``emit_run_cancelled`` 统一取消事件构造的单元测试。

验证 R1 修复的核心不变量：三节点（model / tools / observe）统一经
``helper.common.emit_run_cancelled`` 发出的 ``RUN_CANCELLED`` 事件，必须携带
``langfuse_trace_id`` 与六 token 字段，且口径与模型节点历史实现一致。

这里只测收口函数本身（纯构造 + 写事件），不依赖 LangGraph 运行时，通过
mock ``get_stream_writer`` 捕获写入的事件。
"""

from unittest.mock import MagicMock

import pytest

from app.core.workflows.nodes.helper.common import emit_run_cancelled
from app.core.workflows.react.runtime_config import RuntimeConfig
from app.models.enums.event_type import EventType
from app.models.turn_usage_stats import TurnUsageStats


def _make_rc(langfuse_trace_id: str | None, *, input_tokens: int = 0) -> RuntimeConfig:
    """构造 ``emit_run_cancelled`` 使用的 ``RuntimeConfig`` 实例。

    函数仅读取 ``usage_stats`` 与 ``langfuse_trace_id``，必填的 operations / turn /
    model 用 ``MagicMock`` 占位（不影响被测逻辑），同时保留真实 ``RuntimeConfig`` 类型
    以满足静态检查。
    """
    usage_stats = TurnUsageStats()
    usage_stats.input_tokens = input_tokens
    return RuntimeConfig(
        operations=MagicMock(),
        turn=MagicMock(),
        model=MagicMock(),
        usage_stats=usage_stats,
        langfuse_trace_id=langfuse_trace_id,
    )


def test_emit_run_cancelled_carries_langfuse_trace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """取消事件必须携带 langfuse_trace_id，避免 Langfuse 追踪断链（R1 核心）。"""
    writer = MagicMock()
    monkeypatch.setattr(
        "app.core.workflows.nodes.helper.common.get_stream_writer",
        lambda: writer,
    )

    rc = _make_rc("trace-abc-123")
    emit_run_cancelled(rc, "step-3")

    assert writer.call_count == 1
    event = writer.call_args.args[0]
    assert event["event_type"] == str(EventType.RUN_CANCELLED)
    payload = event["payload"]
    assert payload.langfuse_trace_id == "trace-abc-123"


def test_emit_run_cancelled_none_trace_id_not_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """未启用 tracing（trace id 为 None）时取消事件仍正常发出，不写占位串。"""
    writer = MagicMock()
    monkeypatch.setattr(
        "app.core.workflows.nodes.helper.common.get_stream_writer",
        lambda: writer,
    )

    rc = _make_rc(None)
    emit_run_cancelled(rc, "step-1")

    payload = writer.call_args.args[0]["payload"]
    assert payload.langfuse_trace_id is None


def test_emit_run_cancelled_includes_usage_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    """取消事件需带六 token 字段且与 usage_stats 口径一致。"""
    writer = MagicMock()
    monkeypatch.setattr(
        "app.core.workflows.nodes.helper.common.get_stream_writer",
        lambda: writer,
    )

    rc = _make_rc("trace-x", input_tokens=42)
    emit_run_cancelled(rc, "step-2")

    payload = writer.call_args.args[0]["payload"]
    # total_tokens 是 TurnUsageStats 的独立累计字段（非 input+output 派生），
    # 仅设置 input_tokens 时仍为零值，验证 emit_run_cancelled 忠实透传 usage_stats。
    assert payload.input_tokens == 42
    assert payload.output_tokens == 0
    assert payload.total_tokens == 0
    assert payload.cache_hit_tokens == 0
    assert payload.cache_miss_tokens == 0
    assert payload.reasoning_tokens == 0
    assert payload.status == "cancelled"
    assert payload.error == "turn_cancelled"
    assert payload.step_id == "step-2"
