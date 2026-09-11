"""工具调用修复三种组合的工作流行为测试。

重点验证协议消息顺序，而不是 provider 的具体 chunk 形状：
合法调用必须继续执行；全可修复非法调用必须回流 model；部分有效调用的修复提示
必须延迟到全部 ToolMessage 之后。
"""

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, SystemMessage, ToolMessage

from app.core.workflows.nodes import model_node as model_module
from app.core.workflows.nodes import observation_node as observe_module
from app.core.workflows.nodes.helper import tool_call_lifecycle as lifecycle_module
from app.core.workflows.nodes.helper.model_chunk import ModelChunkProcessor
from app.core.workflows.nodes.helper.tool_call_lifecycle import (
    ToolCallLifecycleManager,
    ToolCallLifecycleRecord,
)
from app.core.workflows.react.state import ReactGraphState


def _state(**overrides: Any) -> ReactGraphState:
    """构造最小可执行的 ReAct graph state。"""

    values: dict[str, Any] = {
        "step_count": 1,
        "tool_error_count": 0,
        "requested_tool": False,
        "repair_requested": False,
        "final_response": False,
        "terminal": False,
        "pending_tool_calls": {},
        "max_steps": 10,
        "final_text": "",
        "last_tool_results": {"instruction": "", "observations": []},
        "deferred_repair_message": "",
    }
    values.update(overrides)
    return ReactGraphState(**values)


class _ModelHarness:
    """为 model 节点提供最小运行时依赖。"""

    def __init__(self, message: AIMessage, chunks: list[AIMessageChunk] | None = None) -> None:
        self.messages: list[Any] = []
        self.events: list[Any] = []
        self._message = message
        self._chunks = chunks or [AIMessageChunk(content="")]
        self.operations = SimpleNamespace(
            model_tools=[SimpleNamespace(name="read_file", display=None)],
            get_current_run=lambda: SimpleNamespace(task_id=1, id=2),
            is_current_run_cancelled=lambda: False,
        )
        self.runtime_config = SimpleNamespace(
            operations=self.operations,
            model=SimpleNamespace(astream=self._astream),
            thinking_channel="",
            thinking_roundtrip=True,
            run=SimpleNamespace(task_id=1, id=2),
            usage_stats=SimpleNamespace(
                add_usage_metadata=lambda _metadata: None,
                to_dict=lambda: {},
            ),
        )
        self.runtime_context = SimpleNamespace(
            load_message=lambda: [],
            add_message=self.messages.append,
        )

    async def _astream(self, _messages: list[Any]) -> AsyncIterator[AIMessageChunk]:
        """返回一个占位 chunk；collector 在测试中被替换为固定消息。"""

        for chunk in self._chunks:
            yield chunk


def _patch_lifecycle_runtime(monkeypatch: Any, harness: Any) -> None:
    """把 lifecycle 的 graph runtime 依赖绑定到测试桩。"""

    monkeypatch.setattr(lifecycle_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(lifecycle_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(lifecycle_module, "get_stream_writer", lambda: harness.events.append)

def test_reasoning_closes_before_tool_call_created(monkeypatch: Any) -> None:
    """工具创建前必须先收口仍在运行的 reasoning part。"""

    message = AIMessage(
        content="",
        tool_calls=[{"name": "read_file", "args": {}, "id": "call-1"}],
    )
    chunks = [
        AIMessageChunk(content="", additional_kwargs={"reasoning_content": "先检查文件"}),
        AIMessageChunk(
            content="",
            tool_call_chunks=[{"name": "read_file", "args": "{}", "id": "call-1", "index": 0}],
        ),
    ]
    harness = _ModelHarness(message, chunks)
    harness.runtime_config.thinking_channel = "reasoning_content"
    monkeypatch.setattr(model_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)
    monkeypatch.setattr(ModelChunkProcessor, "collect", lambda _self, _chunks: message)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["requested_tool"] is True
    closed_index = next(
        index
        for index, event in enumerate(harness.events)
        if event.type == "assistant_part_closed"
    )
    created_index = next(
        index
        for index, event in enumerate(harness.events)
        if event.type == "tool_call_created"
    )
    assert closed_index < created_index
    assert harness.events[closed_index].part == "reasoning"


def test_multiple_tool_calls_are_created_and_started_independently(monkeypatch: Any) -> None:
    """同一模型响应中的多个 tool_call 必须按 index 分别累积和发射生命周期事件。"""

    message = AIMessage(
        content="",
        tool_calls=[
            {"name": "read_file", "args": {"path": "a.py"}, "id": "call-a"},
            {"name": "read_file", "args": {"path": "b.py"}, "id": "call-b"},
        ],
    )
    chunks = [
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"name": "read_file", "args": '{"path":', "id": "call-a", "index": 0},
                {"name": "read_file", "args": '{"path":', "id": "call-b", "index": 1},
            ],
        ),
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"name": None, "args": '"a.py"}', "id": None, "index": 0},
                {"name": None, "args": '"b.py"}', "id": None, "index": 1},
            ],
        ),
    ]
    harness = _ModelHarness(message, chunks)
    monkeypatch.setattr(model_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)
    monkeypatch.setattr(ModelChunkProcessor, "collect", lambda _self, _chunks: message)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["requested_tool"] is True
    lifecycle_events = [
        event
        for event in harness.events
        if event.type in {"tool_call_created", "tool_call_status_changed"}
    ]
    assert [(event.type, event.tool_call_id) for event in lifecycle_events] == [
        ("tool_call_created", "call-a"),
        ("tool_call_created", "call-b"),
        ("tool_call_status_changed", "call-a"),
        ("tool_call_status_changed", "call-b"),
    ]
    status_args = [
        event.args for event in lifecycle_events if event.type == "tool_call_status_changed"
    ]
    assert status_args == [{"path": "a.py"}, {"path": "b.py"}]


def test_all_repairable_invalid_calls_route_back_to_model(monkeypatch: Any) -> None:
    """全无效但可识别的调用应追加修复提示并设置 repair_requested。"""

    message = AIMessage(
        content="",
        invalid_tool_calls=[
            {"name": "read_file", "args": "{", "error": "invalid json"}
        ],
    )
    harness = _ModelHarness(message)
    monkeypatch.setattr(model_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: harness.events.append)
    monkeypatch.setattr(ModelChunkProcessor, "collect", lambda _self, _chunks: message)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["repair_requested"] is True
    assert result["requested_tool"] is False
    assert isinstance(harness.messages[-1], SystemMessage)
    assert "Retry this step" in harness.messages[-1].content


def test_partial_valid_calls_defer_repair_until_after_tool_messages(monkeypatch: Any) -> None:
    """部分有效调用不得在 AIMessage 与 ToolMessage 之间插入 SystemMessage。"""

    message = AIMessage(
        content="先读取文件。",
        tool_calls=[{"name": "read_file", "args": {}, "id": "valid-1"}],
        invalid_tool_calls=[
            {"name": "read_file", "args": "{", "error": "invalid json"}
        ],
    )
    model_harness = _ModelHarness(message)
    monkeypatch.setattr(model_module, "_runtime_config", lambda: model_harness.runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: model_harness.runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: model_harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, model_harness)
    monkeypatch.setattr(ModelChunkProcessor, "collect", lambda _self, _chunks: message)

    model_result = asyncio.run(model_module._model_node(_state()))
    deferred = model_result["deferred_repair_message"]
    assert deferred
    assert model_result["requested_tool"] is True
    assert [type(item) for item in model_harness.messages] == [AIMessage]

    observe_harness = SimpleNamespace(
        messages=model_harness.messages,
        events=[],
        operations=SimpleNamespace(
            get_current_task=lambda: SimpleNamespace(id=1),
            get_current_run=lambda: SimpleNamespace(id=2),
            is_current_run_cancelled=lambda: False,
            to_tool_model_message=lambda observation: ToolMessage(
                content=observation.content, tool_call_id=observation.tool_call_id
            ),
        ),
    )
    runtime_config = SimpleNamespace(operations=observe_harness.operations, usage_stats=None)
    runtime_context = SimpleNamespace(add_message=observe_harness.messages.append)
    observe_harness.runtime_config = runtime_config
    observe_harness.runtime_context = runtime_context
    monkeypatch.setattr(observe_module, "_runtime_config", lambda: runtime_config)
    monkeypatch.setattr(observe_module, "_runtime_context", lambda: runtime_context)
    _patch_lifecycle_runtime(monkeypatch, observe_harness)

    observe_result = asyncio.run(
        observe_module._observe_node(
                _state(
                    step_count=model_result["step_count"],
                    requested_tool=True,
                    tool_call_lifecycle=model_result["tool_call_lifecycle"],
                    deferred_repair_message=deferred,
                last_tool_results={
                    "instruction": "先读取文件。",
                    "expected_call_ids": ["valid-1"],
                    "observations": [
                        {
                            "tool_call_id": "valid-1",
                            "tool_name": "read_file",
                            "status": "success",
                            "error": "",
                            "reason": "",
                            "content": "file body",
                            "retryable": False,
                            "display_data": {},
                        },
                        {
                            "tool_call_id": "unexpected-1",
                            "tool_name": "read_file",
                            "status": "success",
                            "error": "",
                            "reason": "",
                            "content": "must be dropped",
                            "retryable": False,
                            "display_data": {},
                        },
                    ],
                },
            )
        )
    )

    assert observe_result["deferred_repair_message"] == ""
    assert [type(item) for item in observe_harness.messages] == [
        AIMessage,
        ToolMessage,
        SystemMessage,
    ]
    assert observe_harness.messages[1].tool_call_id == "valid-1"


def test_all_valid_calls_have_no_deferred_repair(monkeypatch: Any) -> None:
    """全有效调用保持原工具分支，不引入修复提示。"""

    message = AIMessage(
        content="读取文件。",
        tool_calls=[{"name": "read_file", "args": {}, "id": "valid-1"}],
    )
    harness = _ModelHarness(message)
    monkeypatch.setattr(model_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)
    monkeypatch.setattr(ModelChunkProcessor, "collect", lambda _self, _chunks: message)

    result = asyncio.run(
        model_module._model_node(
            _state(
                tool_call_lifecycle=ToolCallLifecycleManager(
                    calls={
                        "previous-call": ToolCallLifecycleRecord(
                            tool_call_id="previous-call",
                            tool_name="read_file",
                            status="completed",
                        )
                    }
                )
            )
        )
    )

    assert result["requested_tool"] is True
    assert result["deferred_repair_message"] == ""
    assert result["continuation_error_data"] is None
    assert set(result["tool_call_lifecycle"].calls) == {"valid-1"}
