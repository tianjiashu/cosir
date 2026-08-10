"""Tests for Langfuse tool trace recorder failure boundaries."""

from types import TracebackType
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.core.observability import langfuse_tool_trace_recorder
from app.core.observability.langfuse_tool_trace_recorder import (
    LangfuseToolTraceRecorder,
    build_tool_trace_recorder,
)
from app.service.tool_execution.tool_trace_recorder import _NullToolSpan, _NullToolTraceRecorder
from app.tools.schemas import ToolCall, ToolObservation


class _RecordingContextManager:
    """Minimal context manager that records exit arguments for assertions."""

    def __init__(self, span: Any) -> None:
        """Store the span returned by ``__enter__``.

        参数:
            span: ``__enter__`` 返回给 recorder 的 fake Langfuse span。

        返回:
            无。

        异常:
            无。

        副作用:
            初始化用于测试的状态字段。
        """
        self.span = span
        self.exit_args: tuple[Any, Any, Any] | None = None

    def __enter__(self) -> Any:
        """Return the configured fake span."""
        return self.span

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Record the original exception context passed by recorder."""
        self.exit_args = (exc_type, exc, traceback)


def _build_recorder_with_context(context_manager: Any) -> LangfuseToolTraceRecorder:
    """Create a recorder with a fake client without initializing Langfuse."""
    recorder = LangfuseToolTraceRecorder.__new__(LangfuseToolTraceRecorder)
    client = MagicMock()
    client.start_as_current_observation.return_value = context_manager
    recorder._client = client
    return recorder


def test_span_preserves_tool_execution_exception_context() -> None:
    """Tool exceptions must propagate unchanged and reach Langfuse ``__exit__``."""
    context_manager = _RecordingContextManager(MagicMock())
    recorder = _build_recorder_with_context(context_manager)
    call = ToolCall(tool_name="read_file", arguments={"path": "a.txt"}, call_id="call-1")

    with pytest.raises(ValueError, match="tool failed"), recorder.span(call, "step-1"):
        raise ValueError("tool failed")

    assert context_manager.exit_args is not None
    assert context_manager.exit_args[0] is ValueError
    assert isinstance(context_manager.exit_args[1], ValueError)


def test_span_creation_failure_degrades_to_null_span() -> None:
    """Langfuse span creation failure should not block tool execution."""
    recorder = LangfuseToolTraceRecorder.__new__(LangfuseToolTraceRecorder)
    client = MagicMock()
    client.start_as_current_observation.side_effect = RuntimeError("langfuse down")
    recorder._client = client
    call = ToolCall(tool_name="read_file", arguments={}, call_id="call-1")

    with recorder.span(call, "step-1") as span:
        assert isinstance(span, _NullToolSpan)


def test_finalize_failure_does_not_escape_span_context() -> None:
    """Langfuse update failures during finalize should be logged and ignored."""
    span = MagicMock()
    span.update.side_effect = RuntimeError("update failed")
    context_manager = _RecordingContextManager(span)
    recorder = _build_recorder_with_context(context_manager)
    call = ToolCall(tool_name="execute_terminal", arguments={}, call_id="call-1")

    with recorder.span(call, "step-1") as tool_span:
        tool_span.record(
            ToolObservation(
                tool_name="execute_terminal",
                status="error",
                content="password: secret",
                error="api_key: secret",
                tool_call_id="call-1",
                data={"token": "secret"},
            )
        )

    assert context_manager.exit_args == (None, None, None)


def test_span_sanitizes_input_before_starting_observation() -> None:
    """Tool arguments should be sanitized before being sent to Langfuse."""
    context_manager = _RecordingContextManager(MagicMock())
    recorder = _build_recorder_with_context(context_manager)
    call = ToolCall(
        tool_name="web_extract",
        arguments={"Api_Key": "sk-secret-value-12345678901234567890"},
        call_id="call-1",
    )

    with recorder.span(call, "step-1"):
        pass

    _, kwargs = recorder._client.start_as_current_observation.call_args
    assert kwargs["input"]["arguments"]["Api_Key"] == "[REDACTED]"


def test_build_tool_trace_recorder_degrades_to_null_on_init_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public recorder factory should never leak Langfuse init failures."""
    monkeypatch.setattr(langfuse_tool_trace_recorder, "tracing_enabled", lambda: True)

    class _FailingRecorder:
        """Recorder constructor that simulates Langfuse initialization failure."""

        def __init__(self) -> None:
            """Raise during construction."""
            raise RuntimeError("langfuse init failed")

    monkeypatch.setattr(
        langfuse_tool_trace_recorder,
        "LangfuseToolTraceRecorder",
        _FailingRecorder,
    )

    recorder = build_tool_trace_recorder()

    assert isinstance(recorder, _NullToolTraceRecorder)
