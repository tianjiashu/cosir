"""Langfuse ``turn_trace`` trace_id 形态与嵌套契约测试。

固化根因修复：Langfuse v4 基于 OpenTelemetry 后端，trace_id 必须是 32 位小写十六进制
（无连字符），否则 langfuse 内部 ``int(trace_id, 16)`` 解析失败并反复抛出
``ValueError``（"invalid literal for int() with base 16"）。修复 A 后 trace_id 不再预分配，
而是取自根 observation 的实际 OTel trace_id（``LangfuseObservationWrapper.trace_id``，
同为 32 位小写 hex）；``CallbackHandler`` 不传 ``trace_context``、跟随 OTel current context
使 generation 挂到同一根 span 下。本测试锁定该契约，防止回归回 ``str(uuid4())``（带连字符）
或预分配双轨 trace_id。
"""

from unittest.mock import MagicMock, patch

import pytest

from app.core.observability import langfuse_tracing
from app.core.observability.langfuse_tracing import TraceMetadata
from app.utils.trace_infra.ids import is_trace_id


def _enable_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 Settings 切到「启用且密钥齐备」状态，使 ``tracing_enabled`` 通过。"""
    monkeypatch.setattr(langfuse_tracing.Settings, "LANGFUSE_ENABLED", True)
    monkeypatch.setattr(langfuse_tracing.Settings, "LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setattr(langfuse_tracing.Settings, "LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr(langfuse_tracing.Settings, "LANGFUSE_BASE_URL", "http://localhost:3000")


def _stub_langfuse_imports() -> dict[str, MagicMock]:
    """构造覆盖 langfuse 惰性导入的 stub 模块字典。

    返回可直接喂给 ``patch.dict("sys.modules", ...)`` 的模块名→stub 映射。
    ``_build_langfuse_client`` 与 ``propagate_attributes`` / ``CallbackHandler``
    均走函数内惰性 import，因此只需提供这两个模块对象即可。
    """
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


def test_tracing_disabled_yields_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """未启用 tracing 时，``turn_trace`` 必须零开销、trace_id 为 None。"""
    monkeypatch.setattr(langfuse_tracing.Settings, "LANGFUSE_ENABLED", False)
    metadata = TraceMetadata(task_id="t", turn_id="u", agent_id="a")
    with langfuse_tracing.turn_trace(metadata) as result:
        assert result.callbacks == []
        assert result.trace_id is None


def test_enabled_turn_trace_produces_otel_trace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启用 tracing 时，trace_id 取自根 observation 的实际 OTel trace_id（32 位小写 hex）。

    这是 Langfuse v4 / OpenTelemetry 后端的硬性要求；带连字符的 UUID 会在
    langfuse 内部 ``int(trace_id, 16)`` 解析时抛 ``ValueError``。
    修复 A 后不再预分配 ``new_trace_id()``：``CallbackHandler`` 不传 ``trace_context``，
    generation 跟随 OTel current context 挂到根 span 下；``result.trace_id`` 直接读根
    span 的 ``trace_id`` 属性（``LangfuseObservationWrapper`` 暴露，32-hex）。
    """
    _enable_tracing(monkeypatch)
    stubs = _stub_langfuse_imports()

    # 0) 关键时序陷阱：root_cm.__enter__.return_value.trace_id 的 stub 必须放在
    #    `with turn_trace(...)` 之前——turn_trace 的 yield 在求值结果时已执行
    #    `getattr(root_span, "trace_id", None)`；若在 with 块内才设置，result.trace_id
    #    将是 MagicMock，且 is_trace_id / len==32 断言也会失败。
    #    root_cm 的获取路径与 test_turn_trace_exit_failure_does_not_escape 一致。
    root_cm = stubs["langfuse"].Langfuse.return_value.start_as_current_observation.return_value
    root_cm.__enter__.return_value.trace_id = "0123456789abcdef0123456789abcdef"

    metadata = TraceMetadata(task_id="task-1", turn_id="turn-1", agent_id="agent-1")
    with patch.dict("sys.modules", stubs), langfuse_tracing.turn_trace(metadata) as result:
        assert result.trace_id is not None
        assert is_trace_id(
            result.trace_id
        ), f"trace_id 必须是 32 位小写 hex，实际为 {result.trace_id!r}"
        assert "-" not in result.trace_id, "trace_id 不得含连字符"
        assert len(result.trace_id) == 32
        # 1) CallbackHandler 不再传 trace_context（修复 A 核心）
        langchain_mod = stubs["langfuse.langchain"]
        _, kwargs = langchain_mod.CallbackHandler.call_args
        assert "trace_context" not in kwargs or kwargs["trace_context"] is None
        # 2) result.trace_id 等于根 observation 的实际 trace_id（提前 stub 保证）
        assert result.trace_id == "0123456789abcdef0123456789abcdef"
        langfuse_mod = stubs["langfuse"]
        _, client_kwargs = langfuse_mod.Langfuse.call_args
        assert callable(client_kwargs["mask"])
        assert callable(client_kwargs["mask_otel_spans"])


def test_turn_trace_flush_failure_does_not_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Langfuse flush failures after a successful turn must be ignored."""
    _enable_tracing(monkeypatch)
    stubs = _stub_langfuse_imports()
    client = stubs["langfuse"].Langfuse.return_value
    client.flush.side_effect = RuntimeError("flush failed")
    root_cm = client.start_as_current_observation.return_value
    root_cm.__enter__.return_value.trace_id = "0123456789abcdef0123456789abcdef"

    metadata = TraceMetadata(task_id="task-1", turn_id="turn-1", agent_id="agent-1")
    with patch.dict("sys.modules", stubs), langfuse_tracing.turn_trace(metadata) as result:
        # 根 span 已 stub 实际 trace_id，flush 失败不影响 trace_id 产出
        assert result.trace_id == "0123456789abcdef0123456789abcdef"


def test_turn_trace_exit_failure_does_not_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Langfuse context exit failures must not turn a successful run into failure."""
    _enable_tracing(monkeypatch)
    stubs = _stub_langfuse_imports()
    root_cm = stubs["langfuse"].Langfuse.return_value.start_as_current_observation.return_value
    root_cm.__exit__.side_effect = RuntimeError("exit failed")
    root_cm.__enter__.return_value.trace_id = "0123456789abcdef0123456789abcdef"

    metadata = TraceMetadata(task_id="task-1", turn_id="turn-1", agent_id="agent-1")
    with patch.dict("sys.modules", stubs), langfuse_tracing.turn_trace(metadata) as result:
        # 根 span 已 stub 实际 trace_id，退出失败不影响 trace_id 产出
        assert result.trace_id == "0123456789abcdef0123456789abcdef"


def test_turn_trace_callback_handler_failure_cleans_entered_contexts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CallbackHandler init failure should clean entered Langfuse contexts and degrade."""
    _enable_tracing(monkeypatch)
    stubs = _stub_langfuse_imports()
    langchain_mod = stubs["langfuse.langchain"]
    langchain_mod.CallbackHandler.side_effect = RuntimeError("handler failed")
    client = stubs["langfuse"].Langfuse.return_value
    root_cm = client.start_as_current_observation.return_value
    attr_cm = stubs["langfuse"].propagate_attributes.return_value

    metadata = TraceMetadata(task_id="task-1", turn_id="turn-1", agent_id="agent-1")
    with patch.dict("sys.modules", stubs), langfuse_tracing.turn_trace(metadata) as result:
        assert result.callbacks == []
        assert result.trace_id is None

    root_cm.__exit__.assert_called_once()
    attr_cm.__exit__.assert_called_once()


def test_turn_trace_partial_enter_failure_cleans_root_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If attributes enter fails after root enter, the root context must be closed."""
    _enable_tracing(monkeypatch)
    stubs = _stub_langfuse_imports()
    client = stubs["langfuse"].Langfuse.return_value
    root_cm = client.start_as_current_observation.return_value
    attr_cm = stubs["langfuse"].propagate_attributes.return_value
    attr_cm.__enter__.side_effect = RuntimeError("attributes failed")

    metadata = TraceMetadata(task_id="task-1", turn_id="turn-1", agent_id="agent-1")
    with patch.dict("sys.modules", stubs), langfuse_tracing.turn_trace(metadata) as result:
        assert result.callbacks == []
        assert result.trace_id is None

    root_cm.__exit__.assert_called_once()
    attr_cm.__exit__.assert_not_called()
