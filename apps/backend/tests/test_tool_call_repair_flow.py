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

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, SystemMessage, ToolMessage

import app.core.workflows.react.node_helper.tool_call_lifecycle as lifecycle_module
from app.core.runtime.run_result import ToolRunResult
from app.core.workflows.react.nodes import model_node as model_module
from app.core.workflows.react.nodes import observation_node as observe_module
from app.core.workflows.react.nodes import tools_node as tools_module
from app.core.workflows.react.worflow_state.route import ReactRoute
from app.core.workflows.react.worflow_state.state import ReactGraphState
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


@pytest.fixture(autouse=True)
def _patch_task_space(monkeypatch: Any):
    """为 model 节点提供不依赖数据库的延迟消息队列。"""

    class TaskSpace:
        def __init__(self) -> None:
            self.messages: list[Any] = []

        def take_deferred_system_messages(self, **_kwargs: Any) -> list[Any]:
            messages, self.messages = self.messages, []
            return messages

        def defer_system_message(self, message: Any) -> None:
            self.messages.append(message)

        def has_deferred_system_messages(self) -> bool:
            return bool(self.messages)

    task_space = TaskSpace()
    monkeypatch.setattr(task_runtime_spaces, "get_or_create", lambda _task_id: task_space)
    return task_space


def _state(**overrides: Any) -> ReactGraphState:
    """构造最小可执行的 ReAct graph state。"""

    values: dict[str, Any] = {
        "step_count": 1,
        "tool_error_count": 0,
        "next_node": ReactRoute.MODEL,
        "instruction": "",
        "max_steps": 10,
        "final_text": "",
        "last_tool_results": {"instruction": "", "observations": []},
        "tool_request": {},
    }
    values.update(overrides)
    return ReactGraphState(**values)


class _ModelHarness:
    """为 model 节点提供最小运行时依赖。

    只模拟 model 节点实际协作的三方：``operations``（run 终态落定与取消判定）、
    ``model``（``astream`` 产出的 chunk 流）与 ``runtime_context``。``runtime_context``
    按 ``RuntimeContextManager`` 的当前契约实现：``add_message_chunk`` / ``flush_message_chunk``
    返回本 harness 构造时传入的 ``message``（真实 provider 的 chunk 聚合结果与它同形），
    ``finalize_message_chunk`` 一次收口并**如实使用传入的修订版 ``message``**。因此 model 节点的
    后续判定等于构造时传入的 ``message``（即本轮模型最终输出）。
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

        async def run_tool_calls(*_args: Any, **_kwargs: Any) -> ToolRunResult:
            # tools 节点即使没有 running 调用也会调用本入口；桩返回空批次，保持其短路语义。
            return ToolRunResult(observations=[])

        self.operations = SimpleNamespace(
            model_tools=[SimpleNamespace(name="read_file", display=None)],
            all_vaild_tools=[SimpleNamespace(name="read_file", display=None)],
            allows_tools=frozenset({"read_file"}),
            # model / tools 节点按「当前 task 维度」取上下文，故桩必须暴露 get_current_task。
            get_current_task=lambda: SimpleNamespace(id=1),
            get_current_run=lambda: SimpleNamespace(task_id=1, id=2),
            get_current_workspace=lambda: SimpleNamespace(id=1),
            is_current_run_cancelled=lambda: False,
            complete_run_if_running=complete_run_if_running,
            fail_run_if_running=fail_run_if_running,
            run_tool_calls=run_tool_calls,
        )
        self.runtime_config = SimpleNamespace(
            operations=self.operations,
            model=SimpleNamespace(astream=self._astream),
            final_model=SimpleNamespace(astream=self._astream),
            structured_output=None,
            workspace_id=1,
            thinking_channel="",
            # model 节点读 WorkflowOperations.allows_tools 构造生命周期允许集合。
            run=SimpleNamespace(task_id=1, id=2, extra=None),
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
            self._merged_chunk = chunk if self._merged_chunk is None else self._merged_chunk + chunk
            # 假流：本用例把「本轮完整输出」预置为 ``message``（真实 provider 的 chunk 聚合结果与它
            # 同形：finish_reason / tool_calls / usage 都在其中），故累积结果直接返回它；真实的
            # 聚合、节流与固化行为由 manager 单测钉住。
            return self._message

        def flush_message_chunk(
            *,
            stream_id: str,
            run_id: Any = None,
            mode: str = "running",
        ) -> AIMessage | None:
            del stream_id, run_id
            if mode == "cancel":
                self._merged_chunk = None
            return self._message

        def finalize_message_chunk(
            *,
            stream_id: str,
            run_id: Any = None,
            message: AIMessage | None = None,
        ) -> AIMessage:
            del stream_id, run_id
            # 不模拟「省略 message 时固化内存聚合结果」：节点必须交付修订版，省略即契约被破坏。
            if message is None:
                raise AssertionError("model_node must pass the finalized message")
            self._merged_chunk = None
            self.messages.append(message)
            return message

        self.runtime_context = SimpleNamespace(
            load_message=lambda: [],
            add_message=add_message,
            add_message_chunk=add_message_chunk,
            flush_message_chunk=flush_message_chunk,
            finalize_message_chunk=finalize_message_chunk,
        )

    async def _astream(self, _messages: list[Any]) -> AsyncIterator[AIMessageChunk]:
        for chunk in self._chunks:
            yield chunk


def _patch_lifecycle_runtime(monkeypatch: Any, harness: Any) -> None:
    """把 lifecycle 的 graph runtime 依赖绑定到测试桩。"""

    monkeypatch.setattr(lifecycle_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(lifecycle_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(lifecycle_module, "get_stream_writer", lambda: harness.events.append)
    monkeypatch.setattr(tools_module, "_runtime_context", lambda: harness.runtime_context)


def _observe_harness(
    messages: list[Any], events: list[Any], model_tools: list[Any]
) -> SimpleNamespace:
    """为 observe 节点构造最小运行时依赖（复用传入的消息/事件收集器）。"""

    operations = SimpleNamespace(
        model_tools=model_tools,
        all_vaild_tools=model_tools,
        get_current_task=lambda: SimpleNamespace(id=1),
        get_current_run=lambda: SimpleNamespace(id=2),
        is_current_run_cancelled=lambda: False,
        to_tool_model_message=lambda observation: ToolMessage(
            content=observation.content, tool_call_id=observation.tool_call_id
        ),
    )
    runtime_config = SimpleNamespace(
        operations=operations,
        workspace_id=1,
        usage_stats=None,
    )

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

    assert result["next_node"] is ReactRoute.TOOLS
    closed_index = next(
        index for index, event in enumerate(harness.events) if event.type == "assistant_part_closed"
    )
    assert harness.events[closed_index].part == "reasoning"
    assert not [event for event in harness.events if event.type == "tool_call_created"]
    assert result["tool_request"]["tool_calls"][0]["id"] == "call-1"


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

    assert result["next_node"] is ReactRoute.END
    assert harness.completed is True
    assert harness.failed is False
    assert [type(item) for item in harness.messages] == [AIMessage]


def test_truncated_model_output_continues_without_injected_prompt(monkeypatch: Any) -> None:
    """达到长度上限的文本不能完成 Run，直接回到 model 由模型自行延续，不注入续写提示。"""

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

    assert result["next_node"] is ReactRoute.MODEL
    assert harness.completed is False
    assert harness.failed is False
    # length 类截断只表示本轮达到输出上限，由模型自行从已有输出继续；既不就地写入
    # canonical context，也不进 task 级延迟队列。
    assert [type(item) for item in harness.messages] == [AIMessage]
    assert task_runtime_spaces.get_or_create(1).take_deferred_system_messages() == []


def test_missing_finish_reason_does_not_complete_model_output(monkeypatch: Any) -> None:
    """Provider 未返回完成原因时，已有文本也不能直接标记为最终回答。"""

    message = AIMessage(content="未确认完成")
    harness = _ModelHarness(message)
    monkeypatch.setattr(model_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.MODEL
    assert harness.completed is False
    # 续写提示不再就地写入 canonical context，而是经 task 级延迟队列在下一个 model 步注入；
    # 故本轮 harness.messages 只含 AIMessage，提示位于延迟队列。
    assert [type(item) for item in harness.messages] == [AIMessage]
    deferred_system_messages = task_runtime_spaces.get_or_create(1).take_deferred_system_messages()
    assert len(deferred_system_messages) == 1
    assert "finish_reason=missing" in deferred_system_messages[0].content


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

    assert result["next_node"] is ReactRoute.END
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

    assert result["next_node"] is ReactRoute.TOOLS
    assert not [
        event
        for event in harness.events
        if event.type in {"tool_call_created", "tool_call_status_changed"}
    ]
    monkeypatch.setattr(tools_module, "_runtime_config", lambda: harness.runtime_config)
    tool_result = asyncio.run(tools_module._tools_node(_state(tool_request=result["tool_request"])))
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
    assert tool_result["next_node"] is ReactRoute.OBSERVE


def test_all_repairable_invalid_calls_route_back_to_model(monkeypatch: Any) -> None:
    """全无效批次由 tools 收口、追加修复提示并直接回到 model。"""

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
    assert model_result["next_node"] is ReactRoute.TOOLS
    assert [type(item) for item in harness.messages] == [AIMessage]

    monkeypatch.setattr(tools_module, "_runtime_config", lambda: harness.runtime_config)
    tools_result = asyncio.run(
        tools_module._tools_node(
            _state(
                step_count=model_result["step_count"],
                tool_request=model_result["tool_request"],
            )
        )
    )

    assert tools_result["next_node"] is ReactRoute.MODEL
    assert tools_result["tool_rejection_count"] == 1
    assert tools_result["final_answer_only"] is False
    assert tools_result["last_tool_results"]["observations"] == []
    assert [type(item) for item in harness.messages] == [AIMessage, SystemMessage]
    assert "Retry this step" in harness.messages[-1].content
    invalid_records = [
        record
        for record in tools_result["tool_call_lifecycle"].invalid_calls.values()
        if record.invalid_detail is not None
    ]
    assert len(invalid_records) == 1
    assert invalid_records[0].status == "failed"

    second_rejection = asyncio.run(
        tools_module._tools_node(
            _state(
                step_count=model_result["step_count"],
                tool_request=model_result["tool_request"],
                tool_rejection_count=tools_result["tool_rejection_count"],
            )
        )
    )
    assert second_rejection["next_node"] is ReactRoute.MODEL
    assert second_rejection["final_answer_only"] is True

    harness._message = AIMessage(
        content="已尝试工具，但该工具不可用。以下是基于现有信息的答复。",
        response_metadata={"finish_reason": "stop"},
    )
    final_result = asyncio.run(
        model_module._model_node(
            _state(
                step_count=second_rejection.get("step_count", model_result["step_count"]),
                tool_rejection_count=second_rejection["tool_rejection_count"],
                final_answer_only=second_rejection["final_answer_only"],
            )
        )
    )
    assert final_result["next_node"] is ReactRoute.END
    assert final_result["final_text"] == harness._message.content
    assert harness.completed is True


def test_blocked_only_calls_are_closed_and_return_to_model(monkeypatch: Any) -> None:
    """全阻塞调用不执行工具，先闭合协议并带限制提示回到模型。"""

    harness = _ModelHarness(
        AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "args": {}, "id": "blocked-1"}],
        )
    )
    harness.operations.allows_tools = frozenset()
    _patch_lifecycle_runtime(monkeypatch, harness)
    monkeypatch.setattr(model_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: harness.events.append)
    monkeypatch.setattr(tools_module, "_runtime_config", lambda: harness.runtime_config)

    model_result = asyncio.run(model_module._model_node(_state()))
    tools_result = asyncio.run(
        tools_module._tools_node(
            _state(
                step_count=model_result["step_count"],
                tool_request=model_result["tool_request"],
            )
        )
    )

    assert tools_result["next_node"] is ReactRoute.MODEL
    assert tools_result["last_tool_results"]["observations"] == []
    assert [type(message) for message in harness.messages] == [
        AIMessage,
        ToolMessage,
        SystemMessage,
    ]
    assert harness.messages[1].tool_call_id == "blocked-1"
    assert "disabled" in harness.messages[-1].content
    assert harness.events == []


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
    assert model_result["next_node"] is ReactRoute.TOOLS
    assert [type(item) for item in model_harness.messages] == [AIMessage]
    monkeypatch.setattr(tools_module, "_runtime_config", lambda: model_harness.runtime_config)
    tools_result = asyncio.run(
        tools_module._tools_node(_state(tool_request=model_result["tool_request"]))
    )

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
    runtime_config = SimpleNamespace(
        operations=observe_harness.operations,
        workspace_id=1,
        usage_stats=None,
    )

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
                next_node=ReactRoute.TOOLS,
                tool_call_lifecycle=tools_result["tool_call_lifecycle"],
                tool_feedback=tools_result["tool_feedback"],
                last_tool_results={
                    "instruction": "先读取文件。",
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
                    ],
                },
            )
        )
    )

    # observe 先写 ToolMessage，再把批次修复提示写入上下文。
    assert [type(item) for item in observe_harness.messages] == [
        AIMessage,
        ToolMessage,
        SystemMessage,
    ]
    assert "Retry this step" in observe_harness.messages[-1].content
    assert observe_harness.messages[1].tool_call_id == "valid-1"
    # 合法调用成功结算；非法调用被收口为 failed 且不计入错误计数。
    assert observe_result["tool_error_count"] == 0
    repairable_records = [
        record
        for record in observe_result["tool_call_lifecycle"].invalid_calls.values()
        if record.invalid_detail is not None
    ]
    assert repairable_records
    assert all(record.status == "failed" for record in repairable_records)


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

    result = asyncio.run(model_module._model_node(_state()))
    assert result["next_node"] is ReactRoute.TOOLS
    assert [event for event in harness.events if event.type == "tool_call_created"] == []
    monkeypatch.setattr(tools_module, "_runtime_config", lambda: harness.runtime_config)
    tools_result = asyncio.run(
        tools_module._tools_node(_state(tool_request=result["tool_request"]))
    )
    assert tools_result["next_node"] is ReactRoute.OBSERVE
    assert set(tools_result["tool_call_lifecycle"].valid_calls) == {"valid-1"}
