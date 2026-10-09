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
from langchain_core.messages import AIMessage, AIMessageChunk

import app.core.workflows.react.node_helper.tool_call_lifecycle as lifecycle_module
from app.core.workflows.react.nodes import model_node as model_module
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
    }
    values.update(overrides)
    return ReactGraphState(**values)


class _ModelHarness:
    """为 model 节点提供最小运行时依赖。

    只模拟 model 节点实际协作的三方：``operations``（run 终态落定与取消判定）、
    ``model``（``astream`` 产出的 chunk 流）与 ``runtime_context``。``runtime_context``
    按 ``RuntimeContextManager`` 的当前契约实现：``add_message_chunk`` / ``flush_message_chunk``
    返回本 harness 构造时传入的 ``message``（真实 provider 的 chunk 聚合结果与它同形），
    ``flush_message_chunk`` 按 ``running`` / ``cancel`` / ``finalize`` 三态写同一行，其中
    ``finalize`` 一次收口并**如实使用传入的修订版 ``message``**。因此 model 节点的后续判定等于
    构造时传入的 ``message``（即本轮模型最终输出）。
    """

    def __init__(self, message: AIMessage, chunks: list[AIMessageChunk] | None = None) -> None:
        self.messages: list[Any] = []
        self.events: list[Any] = []
        self.completed = False
        self.failed = False
        self._message = message
        # 显式传空列表表示「零 chunk 流」，故不能写成 ``chunks or [...]``（空列表会被默认值吞掉）。
        self._chunks = chunks if chunks is not None else [AIMessageChunk(content="")]
        self._merged_chunk: AIMessageChunk | None = None

        def complete_run_if_running(*_args: Any, **_kwargs: Any) -> object:
            self.completed = True
            return object()

        def fail_run_if_running(*_args: Any, **_kwargs: Any) -> object:
            self.failed = True
            return object()

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
            message: AIMessage | None = None,
        ) -> AIMessage | None:
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


def _patch_lifecycle_runtime(monkeypatch: Any, harness: _ModelHarness) -> None:
    """把 lifecycle 的 graph runtime 依赖绑定到测试桩。"""

    monkeypatch.setattr(lifecycle_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(lifecycle_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(lifecycle_module, "get_stream_writer", lambda: harness.events.append)


def test_empty_model_stream_fails_loudly_without_finalizing(monkeypatch: Any) -> None:
    """模型一个 chunk 都没产出时必须响亮失败，不得固化出一条空 assistant 消息。

    该分支在收口处、早于任何 lifecycle / tools 交互，因此只打 ``model_module`` 的桩即可覆盖。
    """

    harness = _ModelHarness(AIMessage(content=""), chunks=[])
    monkeypatch.setattr(model_module, "_runtime_config", lambda: harness.runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: harness.runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: harness.events.append)

    with pytest.raises(RuntimeError):
        asyncio.run(model_module._model_node(_state()))

    assert harness.messages == [], "空流不得固化任何消息"


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
