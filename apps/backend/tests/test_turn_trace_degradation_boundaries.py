"""``turn_trace`` 降级路径与 trace_id 防御性读取的边界测试。

覆盖本次修复 A（``langfuse_tracing.py``）的以下契约：
1. 客户端构造失败（``_build_langfuse_client`` 抛异常）→ 降级 yield ``TurnTraceResult([], None)``，
   绝不中断 turn 执行。
2. 根 observation 无 ``trace_id`` 属性（或为 None）→ ``getattr(root_span, "trace_id", None)``
   防御返回 None，callbacks 仍为 ``[handler]``（不因 trace_id 缺失丢弃 handler）。
3. ``tracing_enabled()`` 因缺密钥返回 False → 零开销 yield 空结果。
"""

from unittest.mock import MagicMock, patch

import pytest

from app.core.observability import langfuse_tracing
from app.core.observability.langfuse_tracing import TraceMetadata, turn_trace


class _SpanWithoutTraceId:
    """模拟无 ``trace_id`` 属性的根 span 对象（真实对象，非 MagicMock）。

    MagicMock 的 ``getattr`` 会命中任意属性并返回 MagicMock，无法验证
    ``getattr(root_span, "trace_id", None)`` 的防御默认值，故用普通对象。
    """


def _enable_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(langfuse_tracing.Settings, "LANGFUSE_ENABLED", True)
    monkeypatch.setattr(langfuse_tracing.Settings, "LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setattr(langfuse_tracing.Settings, "LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr(langfuse_tracing.Settings, "LANGFUSE_BASE_URL", "http://localhost:3000")


def _stub_langfuse_imports() -> dict[str, MagicMock]:
    langfuse_mod = MagicMock()
    client = MagicMock()
    root_cm = MagicMock()
    attr_cm = MagicMock()
    client.start_as_current_observation.return_value = root_cm
    langfuse_mod.Langfuse.return_value = client
    langfuse_mod.propagate_attributes.return_value = attr_cm

    langchain_mod = MagicMock()
    handler = MagicMock()
    langchain_mod.CallbackHandler.return_value = handler

    return {
        "langfuse": langfuse_mod,
        "langfuse.langchain": langchain_mod,
    }


def test_turn_trace_client_construction_failure_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """测试目的：客户端构造异常（_build_langfuse_client 抛错）时降级，不中断、无 handler。

    可能发现的缺陷：首次 try 块异常未捕获导致 turn_trace 向上抛 / 半初始化状态泄漏。
    """
    _enable_tracing(monkeypatch)
    stubs = _stub_langfuse_imports()
    monkeypatch.setattr(
        langfuse_tracing,
        "_build_langfuse_client",
        lambda: (_ for _ in ()).throw(RuntimeError("langfuse client down")),
    )
    metadata = TraceMetadata(task_id="t1", turn_id="u1", agent_id="a1")

    with patch.dict("sys.modules", stubs), turn_trace(metadata) as result:
        assert result.callbacks == []
        assert result.trace_id is None


def test_turn_trace_root_span_without_trace_id_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """测试目的：根 span 无 trace_id 属性时 getattr 防御返回 None，且不丢弃 handler。

    可能发现的缺陷：修复 A 后直接访问 ``root_span.trace_id`` 抛 AttributeError 中断
    yield，或 trace_id 缺失时错误地把 callbacks 一并降级为空。
    """
    _enable_tracing(monkeypatch)
    stubs = _stub_langfuse_imports()
    root_cm = stubs["langfuse"].Langfuse.return_value.start_as_current_observation.return_value
    root_cm.__enter__.return_value = _SpanWithoutTraceId()

    metadata = TraceMetadata(task_id="t1", turn_id="u1", agent_id="a1")
    with patch.dict("sys.modules", stubs), turn_trace(metadata) as result:
        assert result.trace_id is None
        assert len(result.callbacks) == 1, "trace_id 缺失不应丢弃已构造的 CallbackHandler"


def test_turn_trace_root_span_trace_id_explicit_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """测试目的：根 span 的 trace_id 属性为 None 时同样返回 None。

    可能发现的缺陷：对 None trace_id 的边界处理（如误报 32-hex 校验失败）。
    """
    _enable_tracing(monkeypatch)
    stubs = _stub_langfuse_imports()
    root_cm = stubs["langfuse"].Langfuse.return_value.start_as_current_observation.return_value
    root_cm.__enter__.return_value.trace_id = None

    metadata = TraceMetadata(task_id="t1", turn_id="u1", agent_id="a1")
    with patch.dict("sys.modules", stubs), turn_trace(metadata) as result:
        assert result.trace_id is None
        assert len(result.callbacks) == 1


def test_turn_trace_missing_keys_degrades_even_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """测试目的：LANGFUSE_ENABLED=True 但缺 public/secret key 时 tracing_enabled 为 False，
    turn_trace 仍 yield 空结果（零开销路径）。

    可能发现的缺陷：密钥缺失分支未降级，仍尝试构造 Langfuse 客户端报错。
    """
    monkeypatch.setattr(langfuse_tracing.Settings, "LANGFUSE_ENABLED", True)
    monkeypatch.setattr(langfuse_tracing.Settings, "LANGFUSE_PUBLIC_KEY", None)
    monkeypatch.setattr(langfuse_tracing.Settings, "LANGFUSE_SECRET_KEY", None)
    metadata = TraceMetadata(task_id="t1", turn_id="u1", agent_id="a1")

    with turn_trace(metadata) as result:
        assert result.callbacks == []
        assert result.trace_id is None


def test_turn_trace_missing_trace_id_logs_warning_with_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """测试目的：根 span 无 trace_id 属性时，新增 warning 路径被触发，事件名与
    turn_id/task_id 定位字段完整，且降级行为不变（trace_id=None、callbacks 保留）。

    可能发现的缺陷：warning 路径缺失 / data 字段拼错无法定位 /
    记 warning 时误把 callbacks 一并降级为空。
    """
    _enable_tracing(monkeypatch)
    stubs = _stub_langfuse_imports()
    root_cm = stubs["langfuse"].Langfuse.return_value.start_as_current_observation.return_value
    root_cm.__enter__.return_value = _SpanWithoutTraceId()

    warning = MagicMock()
    monkeypatch.setattr(langfuse_tracing.log, "warning", warning)

    metadata = TraceMetadata(task_id="t1", turn_id="u1", agent_id="a1")
    with patch.dict("sys.modules", stubs), turn_trace(metadata) as result:
        assert result.trace_id is None
        assert len(result.callbacks) == 1, "warning 不应丢弃已构造的 CallbackHandler"

    warning.assert_called_once()
    args, kwargs = warning.call_args
    assert args == ("langfuse_turn_trace_missing_root_trace_id",)
    data = kwargs["extra"]["data"]
    assert data["turn_id"] == "u1"
    assert data["task_id"] == "t1"


def test_turn_trace_present_trace_id_does_not_log_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """测试目的：根 span 有 trace_id 时不触发缺失 warning（对照组，防误报）。

    可能发现的缺陷：warning 在正常路径被误触发，污染日志。
    """
    _enable_tracing(monkeypatch)
    stubs = _stub_langfuse_imports()
    root_cm = stubs["langfuse"].Langfuse.return_value.start_as_current_observation.return_value
    root_cm.__enter__.return_value.trace_id = "0123456789abcdef0123456789abcdef"

    warning = MagicMock()
    monkeypatch.setattr(langfuse_tracing.log, "warning", warning)

    metadata = TraceMetadata(task_id="t1", turn_id="u1", agent_id="a1")
    with patch.dict("sys.modules", stubs), turn_trace(metadata) as result:
        assert result.trace_id == "0123456789abcdef0123456789abcdef"
        assert len(result.callbacks) == 1

    warning.assert_not_called()
