"""``langfuse_tracing`` 其余降级/防御分支的边界测试（补齐覆盖率）。

覆盖：
1. ``tracing_enabled`` 的 ImportError 分支（langfuse 未安装 → False）。
2. ``_warn_once`` 事件去重（同事件只记一次日志）。
3. ``_mask_langfuse_data`` 脱敏失败 → 保守占位 "[REDACTED]"。
4. ``_mask_langfuse_otel_spans`` 正常打补丁 / 无补丁返回 None / 异常返回 None。
5. ``flush_langfuse`` 未启用直接返回、flush 异常被吞。
"""

import sys
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from app.core.observability import langfuse_tracing
from app.core.observability.langfuse_tracing import (
    _mask_langfuse_data,
    _mask_langfuse_otel_spans,
    flush_langfuse,
    tracing_enabled,
)


def test_tracing_enabled_import_error_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试目的：langfuse 包不可导入时 tracing_enabled 返回 False（不崩溃）。

    可能发现的缺陷：ImportError 分支缺失导致未安装 langfuse 时报错。
    """
    monkeypatch.setattr(langfuse_tracing.Settings, "LANGFUSE_ENABLED", True)
    monkeypatch.setattr(langfuse_tracing.Settings, "LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setattr(langfuse_tracing.Settings, "LANGFUSE_SECRET_KEY", "sk")
    # sys.modules 中值为 None 的条目会使 import 抛 ImportError（标准库行为）
    monkeypatch.setitem(sys.modules, "langfuse", None)
    try:
        assert tracing_enabled() is False
    finally:
        monkeypatch.setitem(sys.modules, "langfuse", MagicMock())


def test_warn_once_deduplicates_same_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试目的：同一事件名只记一次 warning，不重复轰炸日志。

    可能发现的缺陷：去重逻辑失效导致每次调用都写日志。
    """
    monkeypatch.setattr(langfuse_tracing, "_TRACING_WARNING_EVENTS", set())
    warning = MagicMock()
    monkeypatch.setattr(langfuse_tracing.log, "warning", warning)

    langfuse_tracing._warn_once("test_dedup_event", "first")
    langfuse_tracing._warn_once("test_dedup_event", "second")

    assert warning.call_count == 1


def test_mask_langfuse_data_sanitize_failure_returns_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """测试目的：脱敏器抛异常时 _mask_langfuse_data 返回 "[REDACTED]" 不向上抛。

    可能发现的缺陷：脱敏异常穿透 SDK 主流程。
    """
    monkeypatch.setattr(
        langfuse_tracing,
        "sanitize_langfuse_payload",
        lambda _value: (_ for _ in ()).throw(RuntimeError("sanitizer broken")),
    )
    assert _mask_langfuse_data(data={"a": 1}) == "[REDACTED]"


def test_mask_otel_spans_string_and_sequence_replaced() -> None:
    """测试目的：OTel span 属性中敏感字符串被替换，普通序列保持不变，int 跳过。

    可能发现的缺陷：字符串/序列脱敏替换逻辑失效（敏感值泄漏到 trace）。
    """

    class _FakeSpan:
        attributes: ClassVar[dict[str, object]] = {
            "token": "sk-secret-value-12345678901234567890",
            "count": 3,
            "tags": ["a", "b"],
        }

    params = MagicMock(spans={"span-id": _FakeSpan()})
    result = _mask_langfuse_otel_spans(params=params)

    assert result is not None
    assert result.span_patches["span-id"].set_attributes == {"token": "[REDACTED]"}


def test_mask_otel_spans_no_patches_returns_none() -> None:
    """测试目的：无需要脱敏的属性时返回 None（跳过 patch，避免空操作）。"""

    class _FakeSpan:
        attributes: ClassVar[dict[str, object]] = {"count": 3, "ok": True}

    params = MagicMock(spans={"span-id": _FakeSpan()})
    assert _mask_langfuse_otel_spans(params=params) is None


def test_mask_otel_spans_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试目的：脱敏/构造 patch 抛异常时返回 None，不阻断 OTel 导出线程。

    可能发现的缺陷：mask 异常穿透导出线程。
    """
    monkeypatch.setattr(
        langfuse_tracing,
        "sanitize_langfuse_payload",
        lambda _value: (_ for _ in ()).throw(RuntimeError("sanitizer broken")),
    )

    class _FakeSpan:
        attributes: ClassVar[dict[str, object]] = {
            "token": "sk-secret-value-12345678901234567890"
        }

    params = MagicMock(spans={"span-id": _FakeSpan()})
    assert _mask_langfuse_otel_spans(params=params) is None


def test_flush_langfuse_disabled_returns_early(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试目的：tracing 未启用时 flush_langfuse 直接返回，不构造客户端。"""
    monkeypatch.setattr(langfuse_tracing, "tracing_enabled", lambda: False)
    build = MagicMock()
    monkeypatch.setattr(langfuse_tracing, "_build_langfuse_client", build)
    flush_langfuse()
    build.assert_not_called()


def test_flush_langfuse_client_error_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """测试目的：flush 抛异常时被吞掉记日志，进程退出不因此失败。

    可能发现的缺陷：flush 异常穿透导致关闭流程崩溃。
    """
    monkeypatch.setattr(langfuse_tracing, "tracing_enabled", lambda: True)
    client = MagicMock()
    client.flush.side_effect = RuntimeError("flush boom")
    monkeypatch.setattr(langfuse_tracing, "_build_langfuse_client", lambda: client)
    log_exc = MagicMock()
    monkeypatch.setattr(langfuse_tracing.log, "exception", log_exc)

    flush_langfuse()  # 不抛

    log_exc.assert_called_once()
