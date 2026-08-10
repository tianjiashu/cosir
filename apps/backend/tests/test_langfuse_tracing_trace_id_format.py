"""Langfuse ``turn_trace`` trace_id 形态契约测试。

固化根因修复：Langfuse v4 基于 OpenTelemetry 后端，``trace_context`` 里的
``trace_id`` 必须是 32 位小写十六进制（无连字符），否则 langfuse 内部
``int(trace_id, 16)`` 解析失败并反复抛出 ``ValueError``（"invalid literal for
int() with base 16"）。本项目统一用 ``app.utils.trace_infra.ids.new_trace_id``
生成该形态 id；本测试锁定这一契约，防止回归回 ``str(uuid4())``（带连字符）。
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
    """启用 tracing 时，生成的 trace_id 必须是 32 位小写 hex（无连字符）。

    这是 Langfuse v4 / OpenTelemetry 后端的硬性要求；带连字符的 UUID 会在
    langfuse 内部 ``int(trace_id, 16)`` 解析时抛 ``ValueError``。
    """
    _enable_tracing(monkeypatch)
    stubs = _stub_langfuse_imports()

    metadata = TraceMetadata(task_id="task-1", turn_id="turn-1", agent_id="agent-1")
    with patch.dict("sys.modules", stubs), langfuse_tracing.turn_trace(metadata) as result:
        assert result.trace_id is not None
        assert is_trace_id(
            result.trace_id
        ), f"trace_id 必须是 32 位小写 hex，实际为 {result.trace_id!r}"
        assert "-" not in result.trace_id, "trace_id 不得含连字符"
        assert len(result.trace_id) == 32
        # 注入 workflow 的 CallbackHandler 必须拿到 OTel 形态的 trace_id
        langchain_mod = stubs["langfuse.langchain"]
        _, kwargs = langchain_mod.CallbackHandler.call_args
        assert kwargs["trace_context"]["trace_id"] == result.trace_id
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

    metadata = TraceMetadata(task_id="task-1", turn_id="turn-1", agent_id="agent-1")
    with patch.dict("sys.modules", stubs), langfuse_tracing.turn_trace(metadata) as result:
        assert result.trace_id is not None


def test_turn_trace_exit_failure_does_not_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Langfuse context exit failures must not turn a successful run into failure."""
    _enable_tracing(monkeypatch)
    stubs = _stub_langfuse_imports()
    root_cm = stubs["langfuse"].Langfuse.return_value.start_as_current_observation.return_value
    root_cm.__exit__.side_effect = RuntimeError("exit failed")

    metadata = TraceMetadata(task_id="task-1", turn_id="turn-1", agent_id="agent-1")
    with patch.dict("sys.modules", stubs), langfuse_tracing.turn_trace(metadata) as result:
        assert result.trace_id is not None


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
