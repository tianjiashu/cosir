"""model 节点「本轮输出能否收口」的契约测试。

覆盖：空流必须响亮失败（不固化空消息）、无工具时任意完成原因都结束 Run、空正文仍可完成、
缺失完成原因只保留展示并重试，以及工具批次未完成时不执行工具。

历史：本文件由 ``test_tool_call_repair_flow.py`` 拆分而来。该文件其余用例建立在已移除的契约上
（graph state 的 ``tool_request`` / ``tool_rejection_count`` / ``final_answer_only``、tools 节点承担
工具调用创建与「修复提示」收口、``ToolCallLifecycleManager.invalid_calls`` /
``fail_invalid_tools``）；当前契约是「model 节点登记调用并发出创建事件、tools 节点执行尚未起跑的
调用、observe 节点统一结算并注入修复提示」，由 ``test_tool_call_chain_audit*.py``、
``test_tools_node_execution_contract.py``、``test_wait_user_node_contract.py`` 与
``test_tool_observation_summary.py`` 覆盖，故随契约一并删除。
"""

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from openai.lib.streaming.chat import ChunkEvent
from openai.types.chat import ChatCompletion

import app.core.workflow.node_helper.streaming_part_state_machine as streaming_module
import app.core.workflow.node_helper.tool_call_lifecycle as lifecycle_module
from app.core.llm_provider.message_codec import assistant_message
from app.core.workflow.execution_state.react_route import ReactRoute
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.execution_state.step_context import StepContext
from app.core.workflow.nodes import model_node as model_module
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from tests.sdk_fixtures import sdk_completion, sdk_event


@pytest.fixture(autouse=True)
def _patch_task_space(monkeypatch: Any):
    """为 model 节点提供不依赖数据库的延迟消息队列。"""

    class TaskSpace:
        def __init__(self) -> None:
            self.messages: list[Any] = []

        def take_runtime_notices(self, **_kwargs: Any) -> list[Any]:
            messages, self.messages = self.messages, []
            return messages

        def defer_runtime_notice(self, message: Any) -> None:
            self.messages.append(message)

        def has_runtime_notices(self) -> bool:
            return bool(self.messages)

    task_space = TaskSpace()
    monkeypatch.setattr(task_runtime_spaces, "get_or_create", lambda _task_id: task_space)
    return task_space


def _state(**overrides: Any) -> ReactExecutionState:
    """构造最小可执行的 ReAct graph state。"""

    values: dict[str, Any] = {
        "step_count": 1,
        "tool_error_count": 0,
        "next_node": ReactRoute.MODEL,
        "instruction": "",
        "max_steps": 10,
        "last_tool_results": {"instruction": "", "observations": []},
    }
    values.update(overrides)
    return ReactExecutionState(**values)


class _ModelHarness:
    """节点测试直接注入 SDK 事件与完整快照；真实聚合另由 HTTP mock 测试覆盖。"""

    def __init__(self, message: ChatCompletion, chunks: list[dict[str, Any]] | None = None) -> None:
        self.messages: list[Any] = []
        self.events: list[Any] = []
        self.completed = False
        self.completion_fields: dict[str, Any] = {}
        self.failed = False
        self._message = message
        # 显式传空列表表示「零 chunk 流」，故不能写成 ``chunks or [...]``（空列表会被默认值吞掉）。
        self._chunks = chunks if chunks is not None else [{"content": ""}]
        self.saved_responses = []

        def complete_run_if_running(*_args: Any, **kwargs: Any) -> object:
            self.completed = True
            self.completion_fields = kwargs
            return object()

        def fail_run_if_running(*_args: Any, **_kwargs: Any) -> object:
            self.failed = True
            return object()

        self.operations = SimpleNamespace(
            task_tool_schemas=[],
            model_tools=[SimpleNamespace(name="read_file", display=None)],
            all_vaild_tools=[SimpleNamespace(name="read_file", display=None)],
            allows_tools=frozenset({"read_file"}),
            vaild_tool_name=lambda name: name == "read_file",
            # model / tools 节点按「当前 task 维度」取上下文，故桩必须暴露 get_current_task。
            get_current_task=lambda: SimpleNamespace(id=1),
            get_current_run=lambda: SimpleNamespace(task_id=1, id=2),
            get_current_workspace=lambda: SimpleNamespace(id=1),
            is_current_run_cancelled=lambda: False,
            complete_run_if_running=complete_run_if_running,
            fail_run_if_running=fail_run_if_running,
        )
        self.runtime_config = SimpleNamespace(
            operations=self.operations,
            model=SimpleNamespace(stream=self._astream),
            final_model=SimpleNamespace(stream=self._astream),
            structured_output=None,
            max_steps=10,
            workspace_id=1,
            thinking_channel="",
            # model 节点读 WorkflowOperations.allows_tools 构造生命周期允许集合。
            run=SimpleNamespace(task_id=1, id=2, extra=None),
            usage_stats=SimpleNamespace(
                add_usage_metadata=lambda _metadata: None,
                to_dict=lambda: {},
            ),
            step_context=StepContext(),
        )

        def add_message(message: Any, **_kwargs: Any) -> None:
            self.messages.append(message)

        def save_model_response(snapshot, *, include_in_context, **_kwargs):
            self.saved_responses.append((snapshot, include_in_context))
            if include_in_context:
                self.messages.append(assistant_message(snapshot.choices[0].message))

        self.runtime_context = SimpleNamespace(
            load_message=lambda: [],
            add_message=add_message,
            save_model_response=save_model_response,
        )

    async def _astream(self, _messages: list[Any], **kwargs: Any) -> AsyncIterator[ChunkEvent]:
        for chunk in self._chunks:
            yield sdk_event(chunk, self._message)


def _patch_lifecycle_runtime(monkeypatch: Any, harness: _ModelHarness) -> None:
    """把 lifecycle 的 graph runtime 依赖绑定到测试桩。"""

    monkeypatch.setattr(lifecycle_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(lifecycle_module, "_message_context", lambda: harness.runtime_context)
    monkeypatch.setattr(lifecycle_module, "get_stream_writer", lambda: harness.events.append)


def test_empty_model_stream_fails_loudly_without_finalizing(monkeypatch: Any) -> None:
    """模型一个 chunk 都没产出时必须响亮失败，不得固化出一条空 assistant 消息。

    finally 统一收口展示，但空流不会产生持久化记录。
    """

    harness = _ModelHarness(sdk_completion(content="", finish_reason=None), chunks=[])
    _patch_lifecycle_runtime(monkeypatch, harness)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_message_context", lambda: harness.runtime_context)
    monkeypatch.setattr(streaming_module, "get_stream_writer", lambda: harness.events.append)

    with pytest.raises(RuntimeError):
        asyncio.run(model_module._model_node(_state()))

    assert harness.messages == [], "空流不得固化任何消息"
    assert harness.saved_responses == []


@pytest.mark.parametrize(
    "identities",
    [
        [("", "read_file")],
        [("   ", "read_file")],
        [("call", "")],
        [("call", "read_file"), ("call", "read_file")],
        [("call", "read_file"), ("call", "write_file")],
        [("call", "write_file"), ("call", "read_file")],
    ],
)
def test_invalid_tool_identity_rejects_batch_and_cancels_preview(monkeypatch, identities):
    """完整批次身份错误必须取消草稿和预览，不能覆盖调用或进入工具执行。"""
    response = sdk_completion(
        "说明",
        finish_reason="tool_calls",
        tool_calls=[
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}
            for call_id, name in identities
        ],
    )
    harness = _ModelHarness(response)
    # write_file 已注册但本轮禁用，验证跨 allowed/blocked 桶也不能重复身份。
    harness.operations.all_vaild_tools.append(SimpleNamespace(name="write_file", display=None))
    placeholders = []
    harness.runtime_context.add_message = lambda message, **kwargs: placeholders.append(
        (message, kwargs)
    )
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_message_context", lambda: harness.runtime_context)
    monkeypatch.setattr(streaming_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)

    with pytest.raises(ValueError, match="模型工具调用"):
        asyncio.run(model_module._model_node(_state()))

    assert harness.messages == []
    assert len(harness.saved_responses) == 1
    assert harness.saved_responses[0][1] is False
    assert not harness.completed
    assert len(placeholders) == (1 if len(identities) > 1 else 0)
    for message, metadata in placeholders:
        assert message["tool_call_id"] == "call"
        assert metadata["include_in_context"] is False
        assert metadata["metadata"].status == "cancelled"
    statuses = [
        event.status
        for event in harness.events
        if isinstance(event, lifecycle_module.ToolCallStatusChangedEvent)
    ]
    assert statuses == (["cancelled"] if len(identities) > 1 else [])


def test_finish_reason_completes_final_response(monkeypatch: Any) -> None:
    """没有工具调用时，完成原因与最终文本一起交给 Run 完成入口。"""

    message = sdk_completion(content="已完成回答。", finish_reason="stop")
    harness = _ModelHarness(message)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_message_context", lambda: harness.runtime_context)
    monkeypatch.setattr(streaming_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.END
    # 最终回答在产出节点就地落定 completed，不再由 loop 二次落库。
    assert harness.completed is True
    assert harness.failed is False
    assert harness.completion_fields == {"final_output": "已完成回答。", "end_reason": "stop"}
    assert [item["role"] for item in harness.messages] == ["assistant"]


@pytest.mark.parametrize(
    "finish_reason", ["length", "content_filter", "tool_calls", "provider_custom"]
)
def test_any_finish_reason_without_tool_calls_completes_run(
    monkeypatch: Any, finish_reason: str
) -> None:
    """完成原因只要求存在；不按值或正文是否完整做额外判断。"""

    message = sdk_completion(content="回答到一半", finish_reason=finish_reason)
    harness = _ModelHarness(message)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_message_context", lambda: harness.runtime_context)
    monkeypatch.setattr(streaming_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.END
    assert harness.completed is True
    assert harness.failed is False
    assert harness.completion_fields == {
        "final_output": "回答到一半",
        "end_reason": finish_reason,
    }
    assert [item["role"] for item in harness.messages] == ["assistant"]
    assert task_runtime_spaces.get_or_create(1).take_runtime_notices() == []


def test_missing_finish_reason_does_not_complete_model_output(monkeypatch: Any) -> None:
    """Provider 未返回完成原因时，已有文本也不能直接标记为最终回答。"""

    message = sdk_completion(content="未确认完成", finish_reason=None)
    harness = _ModelHarness(message)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_message_context", lambda: harness.runtime_context)
    monkeypatch.setattr(streaming_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.MODEL
    assert harness.completed is False
    # 展示行保留，但没有进入下一步模型的 canonical context。
    assert harness.messages == []
    assert [include for _, include in harness.saved_responses] == [False]
    deferred_system_messages = task_runtime_spaces.get_or_create(1).take_runtime_notices()
    assert len(deferred_system_messages) == 1
    assert "no finish_reason" in deferred_system_messages[0].content


def test_empty_content_with_finish_reason_completes_run(monkeypatch: Any) -> None:
    """模型仅返回完成原因时，Run 也应完成并写入空的最终文本。"""

    harness = _ModelHarness(sdk_completion(content="", finish_reason="stop"))
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_message_context", lambda: harness.runtime_context)
    monkeypatch.setattr(streaming_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.END
    assert harness.completed is True
    assert harness.failed is False
    assert harness.completion_fields == {"final_output": "", "end_reason": "stop"}


def test_end_turn_is_accepted_as_normal_finish_reason(monkeypatch: Any) -> None:
    """支持使用 Anthropic 语义的 end_turn 作为正常结束原因。"""

    message = sdk_completion(content="完成。", finish_reason="end_turn")
    harness = _ModelHarness(message)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_message_context", lambda: harness.runtime_context)
    monkeypatch.setattr(streaming_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.END
    assert harness.completed is True


def test_runtime_notice_is_persisted_as_system_message(monkeypatch: Any) -> None:
    """通知经 context 持久化后参与下一次模型请求，不改写用户原文。"""

    from tests.sdk_fixtures import sdk_completion

    base = {"role": "user", "content": "你好"}
    harness = _ModelHarness(sdk_completion(content="完成", finish_reason="stop"))
    harness.runtime_context.load_message = lambda: [base, *harness.messages]
    requested: list[Any] = []
    space = task_runtime_spaces.get_or_create(1)
    monkeypatch.setattr(space, "take_runtime_notices", lambda **kw: "环境已更新")

    async def stream(messages: list[Any], **kwargs: Any) -> AsyncIterator[ChunkEvent]:
        requested.extend(messages)
        async for chunk in harness._astream(messages, **kwargs):
            yield chunk

    harness.runtime_config.model.stream = stream
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_message_context", lambda: harness.runtime_context)
    monkeypatch.setattr(streaming_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)
    result = asyncio.run(model_module._model_node(_state()))
    assert result["next_node"] is ReactRoute.END
    assert requested[-1]["role"] == "system"
    assert requested[-1]["content"] == "环境已更新"
    assert harness.messages[0] == requested[-1]
    assert base["content"] == "你好"


@pytest.mark.parametrize("arguments", ['{"path":', "[1]", "null", '{"n":NaN}'])
def test_invalid_tool_arguments_preserved_and_never_dispatched(monkeypatch, arguments):
    from unittest.mock import AsyncMock

    from app.core.runtime.run_result import ToolRunResult
    from app.core.workflow.nodes import tools_node as tools_module

    response = sdk_completion(
        "",
        finish_reason="tool_calls",
        tool_calls=[
            {
                "id": "call",
                "type": "function",
                "function": {"name": "read_file", "arguments": arguments},
            }
        ],
    )
    harness = _ModelHarness(response)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_message_context", lambda: harness.runtime_context)
    monkeypatch.setattr(streaming_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)
    patch = asyncio.run(model_module._model_node(_state()))
    assert patch["next_node"] == ReactRoute.TOOLS
    assert harness.messages[-1]["tool_calls"][0]["function"]["arguments"] == arguments
    assert patch["tool_call_lifecycle"].valid_calls["call"].argument_error
    harness.operations.run_tool_calls = AsyncMock(return_value=ToolRunResult(observations=[]))
    harness.operations.all_vaild_tools[0].need_HIL = False
    monkeypatch.setattr(tools_module, "_runtime_context", lambda: harness.runtime_config)
    result = asyncio.run(tools_module._tools_node(_state().model_copy(update=patch)))
    calls = harness.operations.run_tool_calls.call_args.args[1]
    assert calls == []
    observations = result["last_tool_results"]["observations"]
    assert observations[0]["status"] == "error"
    assert observations[0]["tool_call_id"] == "call"


@pytest.mark.parametrize("finish_reason", ["length", None])
def test_truncated_tool_batch_is_not_executed_even_with_complete_json(monkeypatch, finish_reason):
    response = sdk_completion(
        "",
        finish_reason=finish_reason,
        tool_calls=[
            {"id": "call", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
        ],
    )
    harness = _ModelHarness(response)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_message_context", lambda: harness.runtime_context)
    monkeypatch.setattr(streaming_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)
    result = asyncio.run(model_module._model_node(_state()))
    assert result["next_node"] == ReactRoute.MODEL
    assert all(m["role"] != "assistant" for m in harness.messages)
    assert task_runtime_spaces.get_or_create(1).has_runtime_notices()
