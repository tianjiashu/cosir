"""工具观察 checkpoint 摘要的单元测试。

覆盖 ``tool_observation_summary`` 的核心契约：
- 摘要字段齐全且为纯原生类型（可落 checkpoint）
- 刻意不承载 ``data``（展示通道数据不进 checkpoint）
- ``content`` / ``error`` / ``reason`` 截断到 ``Settings.TOOL_OBSERVATION_CONTEXT_LIMIT``
- ``instruction`` 缺省归一为空串
"""

import pytest

from app.config.settings import Settings
from app.core.tools.schemas import ToolObservation
from app.core.workflows.nodes.helper.tool_observation_summary import (
    build_tool_result_summaries,
)


def _observation(**overrides: object) -> ToolObservation:
    """构造一条带大体积 ``data`` 的基础观察。

    参数:
        overrides: 覆盖默认字段的键值。

    返回:
        ``ToolObservation`` 实例。

    异常:
        无。

    副作用:
        无。
    """

    base: dict[str, object] = {
        "tool_name": "read_file",
        "status": "success",
        "content": "hello",
        "error": "",
        "reason": "",
        "retryable": False,
        "tool_call_id": "call-1",
        "data": {"items": ["x" * 5000], "diff": {"a": 1}},
    }
    base.update(overrides)
    return ToolObservation(**base)  # type: ignore[arg-type]


def test_summary_fields_and_no_data() -> None:
    """摘要应包含固定字段集合，且完全不携带 ``data``。"""

    result = build_tool_result_summaries([_observation()], instruction="read it")

    assert result["instruction"] == "read it"
    (item,) = result["observations"]
    assert set(item.keys()) == {
        "call_id",
        "tool_name",
        "status",
        "error",
        "reason",
        "content",
        "retryable",
    }
    assert item["call_id"] == "call-1"
    assert item["tool_name"] == "read_file"
    assert item["status"] == "success"
    assert item["content"] == "hello"
    assert item["retryable"] is False


def test_summary_content_truncated_to_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """超限 ``content`` 应被截断并追加明确的截断尾巴。"""

    monkeypatch.setattr(Settings, "TOOL_OBSERVATION_CONTEXT_LIMIT", 60)
    result = build_tool_result_summaries([_observation(content="z" * 100)])

    (item,) = result["observations"]
    assert len(item["content"]) <= 60
    assert item["content"].endswith("[truncated for checkpoint summary]")


def test_summary_tiny_budget_hard_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    """预算小于尾巴长度时退化为硬截断，仍不超限且不抛异常。"""

    monkeypatch.setattr(Settings, "TOOL_OBSERVATION_CONTEXT_LIMIT", 10)
    result = build_tool_result_summaries([_observation(content="z" * 100)])

    (item,) = result["observations"]
    assert len(item["content"]) <= 10


def test_summary_error_and_reason_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """``error`` 与 ``reason`` 也受同一预算约束。"""

    monkeypatch.setattr(Settings, "TOOL_OBSERVATION_CONTEXT_LIMIT", 40)
    result = build_tool_result_summaries(
        [_observation(status="error", error="e" * 200, reason="r" * 200)]
    )

    (item,) = result["observations"]
    assert item["status"] == "error"
    assert len(item["error"]) <= 40
    assert len(item["reason"]) <= 40


def test_summary_instruction_defaults_to_empty() -> None:
    """未提供 instruction 时应归一为空串而非 None。"""

    result = build_tool_result_summaries([_observation()])

    assert result["instruction"] == ""


def test_summary_preserves_order_and_supports_empty_batch() -> None:
    """多条观察顺序保持一致；空批次返回空列表。"""

    first = _observation(tool_call_id="a")
    second = _observation(tool_call_id="b")

    result = build_tool_result_summaries([first, second])
    assert [item["call_id"] for item in result["observations"]] == ["a", "b"]

    empty = build_tool_result_summaries([])
    assert empty == {"instruction": "", "observations": []}
