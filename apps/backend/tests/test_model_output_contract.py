"""model 节点「本轮输出能否收口」的契约测试。

覆盖：空流必须响亮失败（不固化空消息）、只有正常完成原因才能完成 Run、长度截断回到 model 自行
延续、缺失完成原因不得当作最终回答（经 task 级延迟队列注入续写提示）、``end_turn`` 语义等价于正常
结束。

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

import app.core.workflow.node_helper.tool_call_lifecycle as lifecycle_module
from app.core.llm_provider.message_codec import assistant_message
from app.core.workflow.execution_state.route import ReactRoute
from app.core.workflow.execution_state.state import ReactExecutionState
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
        "final_text": "",
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
        self.failed = False
        self._message = message
        # 显式传空列表表示「零 chunk 流」，故不能写成 ``chunks or [...]``（空列表会被默认值吞掉）。
        self._chunks = chunks if chunks is not None else [{"content": ""}]
        self._merged_chunk: ChatCompletion | None = None

        def complete_run_if_running(*_args: Any, **_kwargs: Any) -> object:
            self.completed = True
            return object()

        def fail_run_if_running(*_args: Any, **_kwargs: Any) -> object:
            self.failed = True
            return object()

        self.operations = SimpleNamespace(
            task_tool_schemas=[],
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
        )

        def add_message(message: Any, **_kwargs: Any) -> None:
            self.messages.append(message)

        def save_stream_snapshot(
            chunk: ChatCompletion, *, stream_id: str, run_id: Any = None
        ) -> ChatCompletion:
            del stream_id, run_id
            self._merged_chunk = chunk
            # 假流：本用例把「本轮完整输出」预置为 ``message``（真实 provider 的 chunk 聚合结果与它
            # 同形：finish_reason / tool_calls / usage 都在其中），故累积结果直接返回它；真实的
            # 聚合、节流与固化行为由 manager 单测钉住。
            return assistant_message(self._message.choices[0].message)

        def flush_message_chunk(
            *,
            stream_id: str,
            run_id: Any = None,
            mode: str = "running",
            message: ChatCompletion | None = None,
        ) -> ChatCompletion | None:
            del stream_id, run_id
            if mode == "finalize":
                # 不模拟「省略 message 时固化内存聚合结果」：节点必须交付修订版，省略即契约被破坏。
                if message is None:
                    raise AssertionError("model_node must pass the finalized message")
                self._merged_chunk = None
                self.messages.append(message)
                return message
            if message is not None:
                raise ValueError("message is only accepted in finalize mode")
            if mode == "cancel":
                self._merged_chunk = None
            return assistant_message(self._message.choices[0].message)

        self.runtime_context = SimpleNamespace(
            load_message=lambda: [],
            add_message=add_message,
            save_stream_snapshot=save_stream_snapshot,
            flush_message_chunk=flush_message_chunk,
        )

    async def _astream(self, _messages: list[Any], **kwargs: Any) -> AsyncIterator[ChunkEvent]:
        for chunk in self._chunks:
            yield sdk_event(chunk, self._message)


def _patch_lifecycle_runtime(monkeypatch: Any, harness: _ModelHarness) -> None:
    """把 lifecycle 的 graph runtime 依赖绑定到测试桩。"""

    monkeypatch.setattr(lifecycle_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(lifecycle_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(lifecycle_module, "get_stream_writer", lambda: harness.events.append)


def test_empty_model_stream_fails_loudly_without_finalizing(monkeypatch: Any) -> None:
    """模型一个 chunk 都没产出时必须响亮失败，不得固化出一条空 assistant 消息。

    该分支在收口处、早于任何 lifecycle / tools 交互，因此只打 ``model_module`` 的桩即可覆盖。
    """

    harness = _ModelHarness(sdk_completion(content="", finish_reason=None), chunks=[])
    monkeypatch.setattr(model_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: harness.events.append)

    with pytest.raises(RuntimeError):
        asyncio.run(model_module._model_node(_state()))

    assert harness.messages == [], "空流不得固化任何消息"


def test_normal_finish_reason_is_required_for_final_response(monkeypatch: Any) -> None:
    """没有工具调用时，只有正常 finish_reason 才能完成 Run。"""

    message = sdk_completion(content="已完成回答。", finish_reason="stop")
    harness = _ModelHarness(message)
    monkeypatch.setattr(model_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.END
    assert harness.completed is False
    assert harness.failed is False
    assert [item["role"] for item in harness.messages] == ["assistant"]


def test_truncated_model_output_continues_without_injected_prompt(monkeypatch: Any) -> None:
    """达到长度上限的文本不能完成 Run，直接回到 model 由模型自行延续，不注入续写提示。"""

    message = sdk_completion(content="回答到一半", finish_reason="length")
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
    assert [item["role"] for item in harness.messages] == ["assistant"]
    assert task_runtime_spaces.get_or_create(1).take_runtime_notices() == []


def test_missing_finish_reason_does_not_complete_model_output(monkeypatch: Any) -> None:
    """Provider 未返回完成原因时，已有文本也不能直接标记为最终回答。"""

    message = sdk_completion(content="未确认完成", finish_reason=None)
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
    assert [item["role"] for item in harness.messages] == ["assistant"]
    deferred_system_messages = task_runtime_spaces.get_or_create(1).take_runtime_notices()
    assert len(deferred_system_messages) == 1
    assert "finish_reason=missing" in deferred_system_messages[0].content


def test_end_turn_is_accepted_as_normal_finish_reason(monkeypatch: Any) -> None:
    """支持使用 Anthropic 语义的 end_turn 作为正常结束原因。"""

    message = sdk_completion(content="完成。", finish_reason="end_turn")
    harness = _ModelHarness(message)
    monkeypatch.setattr(model_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)

    result = asyncio.run(model_module._model_node(_state()))

    assert result["next_node"] is ReactRoute.END
    assert harness.completed is False


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
    monkeypatch.setattr(model_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: harness.events.append)
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
    monkeypatch.setattr(model_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)
    patch = asyncio.run(model_module._model_node(_state()))
    assert patch["next_node"] == ReactRoute.TOOLS
    assert harness.messages[-1]["tool_calls"][0]["function"]["arguments"] == arguments
    assert patch["tool_call_lifecycle"].valid_calls["call"].argument_error
    harness.operations.run_tool_calls = AsyncMock(return_value=ToolRunResult(observations=[]))
    harness.operations.all_vaild_tools[0].need_HIL = False
    monkeypatch.setattr(tools_module, "_runtime_config", lambda: harness.runtime_config)
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
    monkeypatch.setattr(model_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: harness.events.append)
    _patch_lifecycle_runtime(monkeypatch, harness)
    result = asyncio.run(model_module._model_node(_state()))
    assert result["next_node"] == ReactRoute.MODEL
    assert all(m["role"] != "assistant" for m in harness.messages)
    assert task_runtime_spaces.get_or_create(1).has_runtime_notices()
