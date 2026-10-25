"""模型输出收口契约：无完成原因的草稿只供展示，工具身份按注册表过滤。"""

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from openai.lib.streaming.chat import ChunkEvent
from openai.types.chat import ChatCompletion

import app.core.workflow.node_helper.streaming_part_state_machine as streaming_module
from app.core.llm_provider.message_codec import assistant_message
from app.models.react_execution_state import ReactExecutionState
from app.models.enums.react_route import ReactRoute
from app.core.workflow.nodes import model_node as model_module
from app.models.enums.conversation_run_status import RunTransition
from tests.runtime_context_stub import run_state_stub, runtime_stub
from tests.sdk_fixtures import sdk_completion, sdk_event


def _state(**overrides: Any) -> ReactExecutionState:
    """构造最小可执行的 ReAct graph state。"""

    values: dict[str, Any] = {
        "step_count": 1,
        "step_limit": 10,
        "next_node": ReactRoute.MODEL,
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

        def transition_run(transition: RunTransition, **kwargs: Any) -> object:
            if transition is RunTransition.COMPLETE:
                self.completed = True
                kwargs.pop("usage_stats")
                self.completion_fields = kwargs
            elif transition is RunTransition.FAIL:
                self.failed = True
            else:
                raise AssertionError(f"unexpected transition {transition}")
            return object()

        self.runtime_config = runtime_stub(
            allows_tools=frozenset({"read_file"}),
            tool_definitions={"read_file": SimpleNamespace(name="read_file", display=None)},
            run_state=run_state_stub(transition=lambda _run_id, transition, **kw: transition_run(
                transition, **kw
            )),
            model=SimpleNamespace(stream=self._astream),
            usage_stats=SimpleNamespace(
                add_usage_metadata=lambda _metadata: None,
                to_dict=lambda: {},
            ),
            event_sink=self.events.append,
        )

        def add_message(message: Any, **_kwargs: Any) -> None:
            self.messages.append(message)

        def save_model_response(snapshot, *, include_in_context, registered_tool_names):
            self.saved_responses.append((snapshot, include_in_context))
            if include_in_context:
                self.messages.append(
                    assistant_message(snapshot.choices[0].message, registered_tool_names)
                )

        self.runtime_context = SimpleNamespace(
            load_message=lambda: [],
            add_message=add_message,
            save_model_response=save_model_response,
            abandon_model_response=lambda: None,
        )

    async def _astream(self, _messages: list[Any], **kwargs: Any) -> AsyncIterator[ChunkEvent]:
        for chunk in self._chunks:
            yield sdk_event(chunk, self._message)


def _patch_model_runtime(monkeypatch: Any, harness: _ModelHarness) -> None:
    """把流式投影依赖绑定到测试桩。"""

    monkeypatch.setattr(streaming_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(streaming_module, "_message_context", lambda: harness.runtime_context)
    monkeypatch.setattr(streaming_module, "emit", harness.events.append)


def test_empty_model_stream_fails_loudly_without_finalizing(monkeypatch: Any) -> None:
    """模型一个 chunk 都没产出时必须响亮失败，不得固化出一条空 assistant 消息。

    finally 统一收口展示，但空流不会产生持久化记录。
    """

    harness = _ModelHarness(sdk_completion(content="", finish_reason=None), chunks=[])
    _patch_model_runtime(monkeypatch, harness)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_message_context", lambda: harness.runtime_context)

    with pytest.raises(RuntimeError):
        asyncio.run(model_module._model_node(_state()))

    assert harness.messages == [], "空流不得固化任何消息"
    assert harness.saved_responses == []


@pytest.mark.parametrize(
    "identities, expected_ids",
    [
        ([("", "read_file")], []),
        ([("   ", "read_file")], []),
        ([("call", "")], []),
        ([("call", "unknown_tool")], []),
        ([("call", "read_file"), ("", "write_file")], ["call"]),
        ([("call", "read_file"), ("call", "write_file")], ["call", "call"]),
    ],
)
def test_model_filters_unusable_calls_without_rejecting_batch(
    monkeypatch, identities, expected_ids
):
    """缺失身份或未注册的调用不进入 context；其余调用由后续执行层处理。"""
    response = sdk_completion(
        "说明",
        finish_reason="tool_calls",
        tool_calls=[
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}
            for call_id, name in identities
        ],
    )
    harness = _ModelHarness(response)
    # 已注册但本轮禁用的调用仍留在模型协议中，由执行层拒绝。
    harness.runtime_config.tool_definitions["write_file"] = SimpleNamespace(
        name="write_file", display=None
    )
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_message_context", lambda: harness.runtime_context)
    _patch_model_runtime(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert [call["id"] for call in harness.messages[-1].get("tool_calls", [])] == expected_ids
    assert result["next_node"] is (ReactRoute.TOOLS if expected_ids else ReactRoute.END)
    assert [included for _, included in harness.saved_responses] == [False, True]


def test_filtered_calls_do_not_prevent_retry_without_finish_reason(monkeypatch):
    response = sdk_completion(
        "",
        finish_reason=None,
        tool_calls=[
            {"id": "unknown", "type": "function", "function": {"name": "other", "arguments": "{}"}}
        ],
    )
    harness = _ModelHarness(response)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_message_context", lambda: harness.runtime_context)
    _patch_model_runtime(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.PREPARE_INPUT
    assert [included for _, included in harness.saved_responses] == [False]


def test_finish_reason_completes_final_response(monkeypatch: Any) -> None:
    """没有工具调用时，完成原因与最终文本一起交给 Run 完成入口。"""

    message = sdk_completion(content="已完成回答。", finish_reason="stop")
    harness = _ModelHarness(message)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_message_context", lambda: harness.runtime_context)
    _patch_model_runtime(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.END
    # 最终回答在产出节点就地落定 completed，不再由 loop 二次落库。
    assert harness.completed is True
    assert harness.failed is False
    assert harness.completion_fields == {"final_output": "已完成回答。", "end_reason": "stop"}
    assert [item["role"] for item in harness.messages] == ["assistant"]


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "provider_custom"])
def test_any_finish_reason_without_tool_calls_completes_run(
    monkeypatch: Any, finish_reason: str
) -> None:
    """完成原因只要求存在；不按值或正文是否完整做额外判断。"""

    message = sdk_completion(content="回答到一半", finish_reason=finish_reason)
    harness = _ModelHarness(message)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_message_context", lambda: harness.runtime_context)
    _patch_model_runtime(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.END
    assert harness.completed is True
    assert harness.failed is False
    assert harness.completion_fields == {
        "final_output": "回答到一半",
        "end_reason": finish_reason,
    }
    assert [item["role"] for item in harness.messages] == ["assistant"]
    assert harness.runtime_config.mailbox.drain() == []


def test_missing_finish_reason_does_not_complete_model_output(monkeypatch: Any) -> None:
    """Provider 未返回完成原因时，已有文本也不能直接标记为最终回答。"""

    message = sdk_completion(content="未确认完成", finish_reason=None)
    harness = _ModelHarness(message)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_message_context", lambda: harness.runtime_context)
    _patch_model_runtime(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.PREPARE_INPUT
    assert harness.completed is False
    # 展示行保留，但没有进入下一步模型的 canonical context。
    assert harness.messages == []
    assert [include for _, include in harness.saved_responses] == [False]
    [notice] = harness.runtime_config.mailbox.drain()
    assert notice.id is None
    assert notice.body.notice_kind == "model_continuation"


def test_empty_content_with_finish_reason_completes_run(monkeypatch: Any) -> None:
    """模型仅返回完成原因时，Run 也应完成并写入空的最终文本。"""

    harness = _ModelHarness(sdk_completion(content="", finish_reason="stop"))
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_message_context", lambda: harness.runtime_context)
    _patch_model_runtime(monkeypatch, harness)

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
    _patch_model_runtime(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.END
    assert harness.completed is True


def test_runtime_notice_is_persisted_as_tagged_user_message(monkeypatch: Any) -> None:
    """prepare_input 把 mailbox 里的通知合成一条带标签的 user 消息经 context 持久化，参与紧随其后
    的模型请求，不改写用户原文。"""
    from contextlib import nullcontext

    from app.core.workflow.nodes import prepare_input_node as prepare_module
    from app.models.task_message import NoticeBody, TaskMessage
    from tests.sdk_fixtures import sdk_completion

    base = {"role": "user", "content": "你好"}
    harness = _ModelHarness(sdk_completion(content="完成", finish_reason="stop"))
    harness.runtime_context.load_message = lambda: [base, *harness.messages]
    requested: list[Any] = []
    harness.runtime_config.mailbox.post(
        TaskMessage.of(NoticeBody(content="环境已更新", notice_kind="test"))
    )
    # Run 内通知没有外部队列行，确认集合为空；事务只是写入边界，不触碰真实数据库。
    monkeypatch.setattr(
        prepare_module,
        "main_session_factory",
        lambda: SimpleNamespace(begin=lambda: nullcontext(None)),
    )

    async def stream(messages: list[Any], **kwargs: Any) -> AsyncIterator[ChunkEvent]:
        requested.extend(messages)
        async for chunk in harness._astream(messages, **kwargs):
            yield chunk

    harness.runtime_config.model.stream = stream
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_message_context", lambda: harness.runtime_context)
    monkeypatch.setattr(prepare_module, "_runtime_context", lambda: harness.runtime_config)
    monkeypatch.setattr(prepare_module, "_message_context", lambda: harness.runtime_context)
    _patch_model_runtime(monkeypatch, harness)
    prepared = asyncio.run(prepare_module._prepare_input_node(_state()))
    assert prepared["next_node"] is ReactRoute.MODEL
    result = asyncio.run(model_module._model_node(_state()))
    assert result["next_node"] is ReactRoute.END
    assert requested[-1]["role"] == "user"
    assert requested[-1]["content"].startswith("<system_notice>")
    assert requested[-1]["content"].endswith("\n环境已更新\n</system_notice>")
    assert harness.messages[0] == requested[-1]
    assert base["content"] == "你好"


@pytest.mark.parametrize("arguments", ['{"path":', "[1]", "null", '{"n":NaN}'])
def test_invalid_tool_arguments_reach_executor_unparsed(monkeypatch, arguments):
    """模型与 tools 节点都不解析参数：原始参数串原样交给执行器，由准入门禁判定。"""
    from unittest.mock import AsyncMock

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
    _patch_model_runtime(monkeypatch, harness)
    patch = asyncio.run(model_module._model_node(_state()))
    assert patch["next_node"] == ReactRoute.TOOLS
    assert harness.messages[-1]["tool_calls"][0]["function"]["arguments"] == arguments
    assert patch["batch"].calls[0]["function"]["arguments"] == arguments
    run_batch = AsyncMock(return_value=[])
    monkeypatch.setattr(tools_module, "run_tool_batch", run_batch)
    harness.runtime_config.tool_definitions["read_file"].need_HIL = False
    monkeypatch.setattr(tools_module, "_runtime_context", lambda: harness.runtime_config)
    asyncio.run(tools_module._tools_node(_state().model_copy(update=patch)))
    (call,) = run_batch.call_args.args[1]
    assert call.call_id == "call"
    assert call.arguments == arguments


@pytest.mark.parametrize("finish_reason", ["length", None])
def test_tool_calls_take_priority_over_finish_reason(monkeypatch, finish_reason):
    """过滤后仍有调用就进入工具节点，参数与完成原因均不在此拦截。"""
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
    _patch_model_runtime(monkeypatch, harness)
    result = asyncio.run(model_module._model_node(_state()))
    assert result["next_node"] is ReactRoute.TOOLS
    assert harness.messages[-1]["tool_calls"][0]["id"] == "call"
    assert [included for _, included in harness.saved_responses] == [False, True]
    assert harness.runtime_config.mailbox.drain() == []
