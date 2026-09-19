"""工具调用修复三种组合的工作流行为测试。

重点验证协议消息顺序，而不是 provider 的具体 chunk 形状：
合法调用必须继续执行；全可修复非法调用由 observe 统一结算并回流 model；部分有效调用的
修复提示必须延迟到全部 ToolMessage 之后。invalid 判定已下沉到 ToolCallLifecycleManager.classify，
model 节点不再即时注入修复提示。
"""

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, SystemMessage, ToolMessage

from app.core.context.runtime_context_manager import _as_ai_message
from app.core.workflows.react.nodes import model_node as model_module, tools_node as tools_module, \
    observation_node as observe_module
from app.core.workflows.react.nodes.helper.tool_call_lifecycle import (
    ToolCallLifecycleManager,
    ToolCallLifecycleRecord, tool_call_lifecycle as lifecycle_module,
)
from app.core.workflows.react.state import ReactGraphState


def _state(**overrides: Any) -> ReactGraphState:
    """构造最小可执行的 ReAct graph state。"""

    values: dict[str, Any] = {
        "step_count": 1,
        "tool_error_count": 0,
        "requested_tool": False,
        "continue_model": False,
        "final_response": False,
        "terminal": False,
        "instruction": "",
        "max_steps": 10,
        "final_text": "",
        "last_tool_results": {"instruction": "", "observations": []},
    }
    values.update(overrides)
    return ReactGraphState(**values)


class _ModelHarness:
    """为 model 节点提供最小运行时依赖。

    只模拟 model 节点实际协作的三方：``operations``（run 终态落定与取消判定）、
    ``model``（``astream`` 产出的 chunk 流）与 ``runtime_context``。``runtime_context``
    按 ``RuntimeContextManager`` 的当前契约实现：``add_message_chunk`` 累积 chunk 并返回
    聚合后的 ``AIMessage``（model 节点用它做流式工具调用登记），
    ``flush_message_chunk(mode="complete")`` 返回收口后的完整 ``AIMessage`` 并计入
    canonical 消息序列——model 节点的后续判定完全基于该返回值，故其等于本 harness 构造时
    传入的 ``message``（即本轮模型最终输出）。
    """

    def __init__(self, message: AIMessage, chunks: list[AIMessageChunk] | None = None) -> None:
        self.messages: list[Any] = []
        self.events: list[Any] = []
        self.completed = False
        self.failed = False
        self._message = message
        self._chunks = chunks or [AIMessageChunk(content="")]
        self._merged_chunk: AIMessageChunk | None = None

        def complete_run_if_running(*_args: Any, **_kwargs: Any) -> object:
            self.completed = True
            return object()

        def fail_run_if_running(*_args: Any, **_kwargs: Any) -> object:
            self.failed = True
            return object()

        self.operations = SimpleNamespace(
            model_tools=[SimpleNamespace(name="read_file", display=None)],
            get_current_run=lambda: SimpleNamespace(task_id=1, id=2),
            is_current_run_cancelled=lambda: False,
            complete_run_if_running=complete_run_if_running,
            fail_run_if_running=fail_run_if_running,
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

        def add_message(message: Any, **_kwargs: Any) -> None:
            self.messages.append(message)

        def add_message_chunk(
            chunk: AIMessageChunk, *, stream_id: str, run_id: Any = None
        ) -> AIMessage:
            del stream_id, run_id
            self._merged_chunk = (
                chunk if self._merged_chunk is None else self._merged_chunk + chunk
            )
            return _as_ai_message(self._merged_chunk)

        def flush_message_chunk(
            *,
            stream_id: str,
            run_id: Any = None,
            mode: str = "running",
        ) -> AIMessage | None:
            del stream_id, run_id
            if mode != "running":
                self._merged_chunk = None
            if mode != "complete":
                return None
            self.messages.append(self._message)
            return self._message

        self.runtime_context = SimpleNamespace(
            load_message=lambda: [],
            add_message=add_message,
            add_message_chunk=add_message_chunk,
            flush_message_chunk=flush_message_chunk,
        )

    async def _astream(self, _messages: list[Any]) -> AsyncIterator[AIMessageChunk]:
        for chunk in self._chunks:
            yield chunk


def _patch_lifecycle_runtime(monkeypatch: Any, harness: Any) -> None:
    """把 lifecycle 的 graph runtime 依赖绑定到测试桩。"""

    monkeypatch.setattr(lifecycle_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(lifecycle_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(lifecycle_module, "get_stream_writer", lambda: harness.events.append)


def _observe_harness(
    messages: list[Any], events: list[Any], model_tools: list[Any]
) -> SimpleNamespace:
    """为 observe 节点构造最小运行时依赖（复用传入的消息/事件收集器）。"""

    operations = SimpleNamespace(
        model_tools=model_tools,
        get_current_task=lambda: SimpleNamespace(id=1),
        get_current_run=lambda: SimpleNamespace(id=2),
        is_current_run_cancelled=lambda: False,
    )
    runtime_config = SimpleNamespace(operations=operations, usage_stats=None)

    def add_message(message: Any, **_kwargs: Any) -> None:
        messages.append(message)

    runtime_context = SimpleNamespace(add_message=add_message, load_message=lambda: [])
    return SimpleNamespace(
        messages=messages,
        events=events,
        operations=operations,
        runtime_config=runtime_config,
        runtime_context=runtime_context,
    )


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

    result = asyncio.run(model_module._model_node(_state()))

    assert result["requested_tool"] is True
    closed_index = next(
        index for index, event in enumerate(harness.events) if event.type == "assistant_part_closed"
    )
    created_index = next(
        index for index, event in enumerate(harness.events) if event.type == "tool_call_created"
    )
    assert closed_index < created_index
    assert harness.events[closed_index].part == "reasoning"


def test_normal_finish_reason_is_required_for_final_response(monkeypatch: Any) -> None:
    """没有工具调用时，只有正常 finish_reason 才能完成 Run。"""

    message = AIMessage(
        content="已完成回答。",
        response_metadata={"finish_reason": "stop"},
    )
    harness = _ModelHarness(message)
    monkeypatch.setattr(model_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["terminal"] is True
    assert result["final_response"] is True
    assert result.get("continue_model", False) is False
    assert harness.completed is True
    assert harness.failed is False
    assert [type(item) for item in harness.messages] == [AIMessage]


def test_truncated_model_output_gets_continuation_prompt(monkeypatch: Any) -> None:
    """达到长度上限的文本不能完成 Run，应追加提示并回到 model。"""

    message = AIMessage(
        content="回答到一半",
        response_metadata={"finish_reason": "length"},
    )
    harness = _ModelHarness(message)
    monkeypatch.setattr(model_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["terminal"] is False
    assert result["final_response"] is False
    assert result["continue_model"] is True
    assert harness.completed is False
    assert harness.failed is False
    assert [type(item) for item in harness.messages] == [AIMessage, SystemMessage]
    assert "truncated" in harness.messages[-1].content


def test_missing_finish_reason_does_not_complete_model_output(monkeypatch: Any) -> None:
    """Provider 未返回完成原因时，已有文本也不能直接标记为最终回答。"""

    message = AIMessage(content="未确认完成")
    harness = _ModelHarness(message)
    monkeypatch.setattr(model_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["terminal"] is False
    assert result["continue_model"] is True
    assert harness.completed is False
    assert [type(item) for item in harness.messages] == [AIMessage, SystemMessage]
    assert "finish_reason=missing" in harness.messages[-1].content


def test_end_turn_is_accepted_as_normal_finish_reason(monkeypatch: Any) -> None:
    """支持使用 Anthropic 语义的 end_turn 作为正常结束原因。"""

    message = AIMessage(
        content="完成。",
        response_metadata={"stop_reason": "end_turn"},
    )
    harness = _ModelHarness(message)
    monkeypatch.setattr(model_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["terminal"] is True
    assert result["final_response"] is True
    assert harness.completed is True


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
    """全无效但可识别的调用应在 observe 节点统一结算并追加修复提示（不再经 model 即时回流）。"""

    message = AIMessage(
        content="",
        invalid_tool_calls=[
            {"name": "read_file", "args": "{", "id": "call-1", "error": "invalid json"}
        ],
    )
    harness = _ModelHarness(message)
    monkeypatch.setattr(model_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)

    model_result = asyncio.run(model_module._model_node(_state()))

    # model 节点不再即时注入 SystemMessage，而是把非法调用挂进 lifecycle 交 observe 结算。
    assert model_result["requested_tool"] is True
    invalid_records = [
        record
        for record in model_result["tool_call_lifecycle"].calls.values()
        if record.invalid_detail is not None
    ]
    assert len(invalid_records) == 1
    assert invalid_records[0].tool_name == "read_file"
    assert model_result["tool_call_lifecycle"].invalid_count == 1
    assert [type(item) for item in harness.messages] == [AIMessage]

    # tools 节点：无 running 调用，直接短路返回空结果。
    monkeypatch.setattr(tools_module, "_runtime_config", lambda: harness.runtime_config)
    tools_result = asyncio.run(
        tools_module._tools_node(_state(tool_call_lifecycle=model_result["tool_call_lifecycle"]))
    )
    assert tools_result["last_tool_results"]["observations"] == []

    # observe 节点：统一结算非法调用（不写 ToolMessage），并追加修复 SystemMessage，
    # 非终态回流 model。
    observe_harness = _observe_harness(
        harness.messages,
        harness.events,
        model_tools=[SimpleNamespace(name="read_file", display=None)],
    )
    monkeypatch.setattr(observe_module, "_runtime_config", lambda: observe_harness.runtime_config)
    monkeypatch.setattr(observe_module, "_runtime_context", lambda: observe_harness.runtime_context)
    _patch_lifecycle_runtime(monkeypatch, observe_harness)

    observe_result = asyncio.run(
        observe_module._observe_node(
            _state(
                step_count=model_result["step_count"],
                requested_tool=True,
                tool_call_lifecycle=tools_result["tool_call_lifecycle"],
                last_tool_results=tools_result["last_tool_results"],
            )
        )
    )
    assert [type(item) for item in observe_harness.messages] == [AIMessage, SystemMessage]
    assert "Retry this step" in observe_harness.messages[-1].content
    # 非法调用已被收口为 failed（前端 pending part 闭合），且不计入 tool_error_count。
    assert observe_result["tool_error_count"] == 0
    assert any(
        record.status == "failed" and record.invalid_detail
        for record in observe_result["tool_call_lifecycle"].calls.values()
    )


def test_partial_valid_calls_defer_repair_until_after_tool_messages(monkeypatch: Any) -> None:
    """部分有效调用不得在 AIMessage 与 ToolMessage 之间插入 SystemMessage。"""

    message = AIMessage(
        content="先读取文件。",
        tool_calls=[{"name": "read_file", "args": {}, "id": "valid-1"}],
        invalid_tool_calls=[
            {"name": "read_file", "args": "{", "id": "invalid-1", "error": "invalid json"}
        ],
    )
    model_harness = _ModelHarness(message)
    monkeypatch.setattr(model_module, "_runtime_config", lambda: model_harness.runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: model_harness.runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: model_harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, model_harness)

    model_result = asyncio.run(model_module._model_node(_state()))
    assert model_result["requested_tool"] is True
    assert [type(item) for item in model_harness.messages] == [AIMessage]

    observe_harness = SimpleNamespace(
        messages=model_harness.messages,
        events=[],
        operations=SimpleNamespace(
            model_tools=[SimpleNamespace(name="read_file", display=None)],
            get_current_task=lambda: SimpleNamespace(id=1),
            get_current_run=lambda: SimpleNamespace(id=2),
            is_current_run_cancelled=lambda: False,
            to_tool_model_message=lambda observation: ToolMessage(
                content=observation.content, tool_call_id=observation.tool_call_id
            ),
        ),
    )
    runtime_config = SimpleNamespace(operations=observe_harness.operations, usage_stats=None)

    def add_message(message: Any, **_kwargs: Any) -> None:
        observe_harness.messages.append(message)

    runtime_context = SimpleNamespace(add_message=add_message)
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

    assert [type(item) for item in observe_harness.messages] == [
        AIMessage,
        ToolMessage,
        SystemMessage,
    ]
    assert observe_harness.messages[1].tool_call_id == "valid-1"
    # 合法调用成功结算；非法调用被收口为 failed 且不计入错误计数。
    assert observe_result["tool_error_count"] == 0
    assert any(
        record.status == "failed" and record.invalid_detail
        for record in observe_result["tool_call_lifecycle"].calls.values()
    )


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
    assert set(result["tool_call_lifecycle"].calls) == {"valid-1"}
