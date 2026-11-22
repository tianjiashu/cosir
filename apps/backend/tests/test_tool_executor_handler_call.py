"""ToolExecutor 调用 handler 的部分：开始前查 Session 级取消、异常收口、结果归一化与 running 时机。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from app.core.tool.execute import tool_executor as tool_executor_module
from app.core.tool.execute.tool_executor import ToolExecutor
from app.core.tool.schemas import ToolCall, ToolDefinition, ToolExecutionContext, ToolObservation
from app.core.tool.schemas.request_tools import RequestTools
from app.core.tool.tool_registry import ToolRegistry
from app.core.workflow.cancel.run_cancellation_registry import cancellation_registry

_SESSION_ID = 1


class _NoArgs(BaseModel):
    """空参数模型：测试工具均不需要入参。"""


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    events: list[Any] = []
    monkeypatch.setattr(tool_executor_module, "emit", events.append)
    return events


@pytest.fixture(autouse=True)
def _clean_session_signal():
    cancellation_registry.consume(_SESSION_ID)
    yield
    cancellation_registry.consume(_SESSION_ID)


def _make_tool(handler, *, name: str = "probe") -> ToolDefinition:
    return ToolDefinition(name=name, group="probe", description="probe", handler=handler, args_model=_NoArgs)


def _execute(handler, *, call_id: str = "call-1") -> ToolObservation:
    context = ToolExecutionContext(
        step_id="step-1", session_id=_SESSION_ID, workspace_id=1, workspace_root=Path("."), run_id=1,
        tool_call_id=call_id,
    )
    return ToolExecutor(ToolRegistry([_make_tool(handler)])).execute(
        ToolCall(tool_name="probe", arguments='{}', call_id=call_id), context,
        RequestTools(schemas=(), admitted=frozenset({"probe"})),
    )


def test_cancelled_before_start_is_reported_as_not_executed(emitted: list[Any]) -> None:
    """Session 已停止：handler 不执行、不发 running，正文如实说调用没跑过，而不是「结果未知」。"""

    ran: list[bool] = []

    def handler(execution_context=None, **_kwargs):
        ran.append(True)
        return "ran"

    cancellation_registry.request_cancel(_SESSION_ID)
    observation = _execute(handler, call_id="call-pre")

    assert ran == []
    assert emitted == []
    assert observation.status == "cancelled"
    assert observation.tool_call_id == "call-pre"
    assert "was not executed and has no effects" in observation.content
    assert "unknown" not in observation.content


def test_normal_execution_emits_running_and_returns_success(emitted: list[Any]) -> None:
    observation = _execute(lambda execution_context=None, **_kwargs: "hello", call_id="call-ok")

    assert [event.result.status for event in emitted] == ["running"]
    assert observation.status == "success"
    assert observation.content == "hello"
    assert observation.tool_call_id == "call-ok"


def test_handler_exception_returns_error(emitted: list[Any]) -> None:
    def handler(execution_context=None, **_kwargs):
        raise RuntimeError("boom-in-handler")

    observation = _execute(handler)

    assert observation.status == "error"
    assert "boom-in-handler" in observation.content


def test_result_is_kept_when_cancel_arrives_during_execution(emitted: list[Any]) -> None:
    """handler 已跑完就保留真实结果：改判为取消会让模型以为写盘等副作用没有发生而重复执行。"""

    def handler(execution_context=None, **_kwargs):
        cancellation_registry.request_cancel(_SESSION_ID)
        return "kept"

    observation = _execute(handler)

    assert observation.status == "success"
    assert observation.content == "kept"


def test_normalize_result_wraps_scalar_and_structured_payloads() -> None:
    """标量走 str()，dict/list 走 json.dumps；ToolObservation 透传并补 tool_call_id。"""

    tool = _make_tool(lambda **_kwargs: None, name="norm")
    normalize = ToolExecutor._normalize_result

    assert normalize(tool, 42, "cid").content == "42"
    assert normalize(tool, {"a": [1, 2]}, "cid2").content == '{"a": [1, 2]}'

    existing = ToolObservation(tool_name="norm", status="success", content="x", tool_call_id="")
    assert normalize(tool, existing, "cid3") == replace(existing, tool_call_id="cid3")
