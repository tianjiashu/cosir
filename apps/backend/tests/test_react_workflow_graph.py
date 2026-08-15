"""ReAct graph 级集成测试（invalid_tool_calls 自愈 Task 3 测试点 11-12）。

用 fake chat model + ``InMemorySaver`` 跑真实编译后的 graph（真实节点 + 真实条件边），
验证 invalid_tool_calls 自愈在端到端路径上真的成立：不误判终态、能回流 model 重试、
合法工具照常执行、重试耗尽由 ``max_steps_node`` 以 ``max_steps_reached`` 兜底。

同时保留 graph 组装契约测试（max_steps 节点与路由已注册）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any
from unittest.mock import MagicMock

from langchain_core.messages import AIMessageChunk, BaseMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END

from app.core.workflows.react import workflow
from app.core.workflows.react.runtime_config import RuntimeConfig
from app.core.workflows.react.state import ReactGraphState
from app.models import RuntimeMessage
from app.models.enums.event_type import EventType
from app.models.turn_usage_stats import TurnUsageStats
from app.service.tool_execution.run_result import ToolRunResult
from app.tools.schemas import ToolCall, ToolObservation


class _FakeChatModel:
    """按预设脚本逐轮产出 chunk 的 fake chat model（只实现 ``astream``）。"""

    def __init__(self, scripted_chunks: Sequence[AIMessageChunk]) -> None:
        """记录每轮要产出的 chunk 脚本。

        参数:
            scripted_chunks: 每轮模型调用产出的 chunk；调用次数超出脚本长度时
                重复最后一条（模拟模型持续输出同样的非法调用）。

        返回:
            无。

        异常:
            无。

        副作用:
            初始化调用计数与收到的消息记录。
        """

        self._scripted_chunks = list(scripted_chunks)
        self.call_count = 0
        self.seen_messages: list[list[BaseMessage]] = []

    async def astream(
        self, messages: list[BaseMessage], **_kwargs
    ) -> AsyncIterator[AIMessageChunk]:
        """产出本轮脚本 chunk，并记录本轮真实收到的上下文消息。

        参数:
            messages: 本轮模型输入上下文（由 ``RuntimeContext.load_message`` 提供）。
            **_kwargs: LangChain 透传参数，本 fake 不使用。

        返回:
            单 chunk 的异步迭代器。

        异常:
            无。

        副作用:
            递增 ``call_count``、追加 ``seen_messages``。
        """

        self.seen_messages.append(list(messages))
        index = min(self.call_count, len(self._scripted_chunks) - 1)
        self.call_count += 1
        yield self._scripted_chunks[index]


class _FakeRuntimeContext:
    """最小 RuntimeContext 替身：内存维护消息列表。"""

    def __init__(self) -> None:
        """初始化空消息列表。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """

        self.messages: list[BaseMessage] = []

    def load_message(self) -> list[BaseMessage]:
        """返回当前消息列表拷贝。

        参数:
            无。

        返回:
            消息列表的独立拷贝。

        异常:
            无。

        副作用:
            无。
        """

        return list(self.messages)

    def add_message(self, message: BaseMessage) -> None:
        """追加一条消息到上下文末端。

        参数:
            message: 待追加的 LangChain 消息。

        返回:
            无。

        异常:
            无。

        副作用:
            修改内部 ``messages``。
        """

        self.messages.append(message)

    def system_message_contents(self) -> list[str]:
        """抽取已写入的 SystemMessage 文本。

        参数:
            无。

        返回:
            SystemMessage 内容字符串列表。

        异常:
            无。

        副作用:
            无。
        """

        return [
            str(message.content)
            for message in self.messages
            if isinstance(message, SystemMessage)
        ]


def _make_chunk(
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    invalid_tool_calls: list[dict[str, Any]] | None = None,
) -> AIMessageChunk:
    """构造一个模型流式分块。

    参数:
        content: 文本内容。
        tool_calls: 合法工具调用列表。
        invalid_tool_calls: 非法工具调用列表。

    返回:
        带 usage 元数据的 ``AIMessageChunk``。

    异常:
        无。

    副作用:
        无。
    """

    return AIMessageChunk(
        content=content,
        id="lc_run--graph",
        tool_calls=tool_calls or [],
        invalid_tool_calls=invalid_tool_calls or [],
        response_metadata={"finish_reason": "tool_calls", "model_name": "fake-model"},
        usage_metadata={
            "input_tokens": 4,
            "output_tokens": 2,
            "total_tokens": 6,
            "input_token_details": {"cache_read": 0},
            "output_token_details": {"reasoning": 0},
        },
    )


def _make_runtime_config(model: _FakeChatModel, tool_names: tuple[str, ...]) -> RuntimeConfig:
    """构造 graph 级测试用的 ``RuntimeConfig``（工具执行返回成功观察）。

    参数:
        model: fake chat model。
        tool_names: 注册给模型的工具名集合。

    返回:
        字段完整的 ``RuntimeConfig``（``approval_resolver=None`` → 工具自动放行）。

    异常:
        无。

    副作用:
        无。
    """

    operations = MagicMock()
    operations.is_current_turn_cancelled.return_value = False
    operations.append_runtime_message.return_value = None
    operations.fail_turn_if_running.return_value = MagicMock()
    operations.complete_turn_if_running.return_value = MagicMock()
    tools = []
    for name in tool_names:
        tool = MagicMock()
        tool.name = name
        tools.append(tool)
    operations.model_tools = tools

    task = MagicMock()
    task.task_id = "task_test"
    turn = MagicMock()
    turn.turn_id = "turn_test"
    operations.get_current_task.return_value = task
    operations.get_current_turn.return_value = turn

    def _run_tool_calls(_task_id, calls, _step_id=None, **_kwargs) -> ToolRunResult:
        """假工具执行：每个调用产出一条成功观察与配对 tool 消息。"""

        return ToolRunResult(
            observations=[
                ToolObservation(
                    tool_name=call.tool_name,
                    status="success",
                    content=f"{call.tool_name} done",
                    tool_call_id=call.call_id,
                )
                for call in calls
            ],
            messages_for_model=[
                RuntimeMessage(
                    role="tool",
                    content_text=f'{{"content": "{call.tool_name} done"}}',
                    metadata={"tool_call_id": call.call_id},
                )
                for call in calls
            ],
        )

    operations.run_tool_calls.side_effect = _run_tool_calls

    return RuntimeConfig(
        operations=operations,
        turn=turn,
        model=model,  # type: ignore[arg-type]
        approval_resolver=None,
        usage_stats=TurnUsageStats(),
        langfuse_trace_id="trace_graph",
    )


async def _run_graph(
    model: _FakeChatModel,
    tool_names: tuple[str, ...],
    max_steps: int,
) -> tuple[list[tuple[EventType, Any]], ReactGraphState, _FakeRuntimeContext, RuntimeConfig]:
    """编译真实 graph 并跑到结束，收集事件与最终 state。

    参数:
        model: fake chat model。
        tool_names: 注册工具名集合。
        max_steps: 步数上限（决定修复回流的兜底时机）。

    返回:
        ``(事件列表, 最终 state, fake runtime_context, runtime_config)`` 四元组。

    异常:
        无。

    副作用:
        以内存 checkpointer 真实执行 graph 节点（含 fake 工具执行与上下文写入）。
    """

    runtime_context = _FakeRuntimeContext()
    rc = _make_runtime_config(model, tool_names)
    graph = workflow.ReactLikeWorkflow()._build_graph(checkpointer=InMemorySaver())
    config = {
        "configurable": {
            "thread_id": "thread_graph_test",
            "runtime_config": rc,
            "runtime_context": runtime_context,
        }
    }
    input_state = ReactGraphState(
        step_count=0,
        tool_error_count=0,
        requested_tool=False,
        repair_requested="false",
        continuation_error_data=None,
        final_response=False,
        terminal=False,
        pending_tool_calls=[],
        max_steps=max_steps,
        final_text="",
        last_tool_results=[],
    )

    events: list[tuple[EventType, Any]] = []
    async for mode, data in graph.astream(input_state, config, stream_mode=["custom"]):
        if mode != "custom":
            continue
        events.append((EventType(data["event_type"]), data["payload"]))

    snapshot = await graph.aget_state(config)
    final_state = ReactGraphState(**snapshot.values)
    return events, final_state, runtime_context, rc


# =============================================================================
# 测试点 11：命中工具名的 invalid_tool_call 且无合法 tool_calls → 回流 + max_steps 兜底
# =============================================================================


async def test_graph_repairs_invalid_tool_call_without_valid_tool(monkeypatch) -> None:
    """测试目的：模型持续产出「命中工具名的 invalid_tool_call 但无合法 tool_calls」时，
    graph 不进入 invalid_output 失败终态，而是反复回流 model，最终由 max_steps 兜底。

    可能发现的缺陷：情形 b 未回流（直接 END / 误落 invalid_model_output 终态），或
    回流未受 max_steps 拦截造成无限循环；也可能终态分类被编造为 parse_invalid。
    """

    model = _FakeChatModel(
        [
            _make_chunk(
                content="",
                tool_calls=[],
                invalid_tool_calls=[
                    {
                        "name": "delegate_task",
                        "args": '{"child_agent_id":"analyst",',
                        "id": "call_bad",
                        "error": "Expecting property name enclosed in double quotes",
                    }
                ],
            )
        ]
    )

    events, final_state, runtime_context, rc = await _run_graph(
        model, tool_names=("delegate_task",), max_steps=3
    )

    # 1) 真的回流重试了（模型被多次调用），而不是一步就 END
    assert model.call_count > 1, "情形 b 必须回流 model 重试"
    # 2) 每轮回流都把修复提示写进上下文，且提示含命中的工具名
    repair_prompts = runtime_context.system_message_contents()
    assert repair_prompts, "必须把修复提示写进 RuntimeContext"
    assert all("delegate_task" in prompt for prompt in repair_prompts)
    # 3) 全程没有 invalid_model_output 误判
    failed = [payload for event_type, payload in events if event_type == EventType.RUN_FAILED]
    assert failed, "重试耗尽后必须有终态失败事件"
    assert all(payload.error != "invalid_model_output" for payload in failed)
    # 4) 终态由 max_steps 兜底，分类为 max_steps_reached
    assert failed[-1].error == "max_steps_reached"
    rc.operations.fail_turn_if_running.assert_called_with(
        "turn_test", end_reason="max_steps_reached"
    )
    # 5) 最终 state 终态且步数达上限
    assert final_state.terminal is True
    assert final_state.step_count >= final_state.max_steps


async def test_model_corrects_itself_on_retry_reaches_final_response(monkeypatch) -> None:
    """测试目的：模型在回流重试中自我纠正（第二轮给出合法输出）时，graph 正常完成。

    可能发现的缺陷：回流后上下文未累积修复提示（模型看不到提示 → 永不纠正），或
    纠正后仍被残留的 ``repair_requested="true"`` 拖住无法结束。
    """

    model = _FakeChatModel(
        [
            _make_chunk(
                content="",
                tool_calls=[],
                invalid_tool_calls=[
                    {"name": "delegate_task", "args": "{", "id": "bad", "error": "bad json"}
                ],
            ),
            _make_chunk(content="修正后的最终答案"),
        ]
    )

    events, final_state, runtime_context, _rc = await _run_graph(
        model, tool_names=("delegate_task",), max_steps=5
    )

    # 第二轮模型确实看到了修复提示（回流把提示带进上下文）
    assert model.call_count >= 2
    second_round_context = model.seen_messages[1]
    assert any(
        isinstance(message, SystemMessage) and "delegate_task" in str(message.content)
        for message in second_round_context
    ), "回流后的上下文必须含修复提示"

    finished = [payload for event_type, payload in events if event_type == EventType.RUN_FINISHED]
    assert finished, "纠正后应正常完成"
    assert final_state.terminal is True
    assert final_state.final_text == "修正后的最终答案"
    assert [
        payload for event_type, payload in events if event_type == EventType.RUN_FAILED
    ] == []


# =============================================================================
# 测试点 12：invalid_tool_calls 与合法 tool_calls 并存（情形 a）
# =============================================================================


async def test_graph_executes_valid_tool_with_repair_hint(monkeypatch) -> None:
    """测试目的：invalid_tool_calls 同时含合法 tool_calls 时，合法工具执行、
    repair 提示随上下文回落、不误判终态（情形 a）。

    可能发现的缺陷：REPAIR 分支 return 终态堵死工具执行（plan §0.1 #3）；或
    路由误把 ``repair_requested="false"`` 当真值导致合法工具永不执行（plan §0.1 #11）。
    """

    model = _FakeChatModel(
        [
            _make_chunk(
                content="我先搜索一下",
                tool_calls=[
                    {"name": "search_files", "args": {}, "id": "call_ok", "type": "tool_call"}
                ],
                invalid_tool_calls=[
                    {
                        "name": "delegate_task",
                        "args": '{"child_agent_id":',
                        "id": "call_bad",
                        "error": "truncated json",
                    }
                ],
            ),
            _make_chunk(content="工具结果已确认，任务完成"),
        ]
    )

    events, final_state, runtime_context, rc = await _run_graph(
        model, tool_names=("search_files", "delegate_task"), max_steps=5
    )

    # 1) 合法工具真的执行了（tools 节点被走到）
    assert rc.operations.run_tool_calls.called, "合法工具必须照常执行"
    executed = rc.operations.run_tool_calls.call_args.args[1]
    assert [call.tool_name for call in executed] == ["search_files"]

    # 2) 修复提示随工具观察一起落进上下文（由 tools_node 经 deferred_repair_message 写入）
    repair_prompts = runtime_context.system_message_contents()
    assert repair_prompts, "修复提示必须回落到上下文"
    assert any("delegate_task" in prompt for prompt in repair_prompts)

    # 3) 不误判终态：正常完成，无失败事件
    assert [
        payload for event_type, payload in events if event_type == EventType.RUN_FAILED
    ] == []
    finished = [payload for event_type, payload in events if event_type == EventType.RUN_FINISHED]
    assert finished
    assert final_state.terminal is True
    assert final_state.final_text == "工具结果已确认，任务完成"


async def test_noise_invalid_with_valid_tool_calls_does_not_inject_repair(monkeypatch) -> None:
    """测试目的：未命中工具名的噪声残片 + 合法工具时，工具执行但**不注入**修复提示。

    可能发现的缺陷：IGNORE 轨误产修复提示 → 每轮都给模型加无意义系统提示，污染上下文。
    """

    model = _FakeChatModel(
        [
            _make_chunk(
                content="",
                tool_calls=[
                    {"name": "search_files", "args": {}, "id": "call_ok", "type": "tool_call"}
                ],
                invalid_tool_calls=[{"name": "", "args": '"', "id": "noise"}],
            ),
            _make_chunk(content="完成"),
        ]
    )

    events, final_state, runtime_context, rc = await _run_graph(
        model, tool_names=("search_files",), max_steps=5
    )

    assert rc.operations.run_tool_calls.called
    assert runtime_context.system_message_contents() == [], "噪声残片不得产出修复提示"
    assert [
        payload for event_type, payload in events if event_type == EventType.RUN_FAILED
    ] == []
    assert final_state.terminal is True


# =============================================================================
# graph 组装契约（既有回归）
# =============================================================================


def test_build_graph_registers_max_steps_node_and_route() -> None:
    """测试目的：编译后的 graph 注册了 ``max_steps`` 节点与 model 的条件路由。

    可能发现的缺陷：漏注册 max_steps 节点或路由 → 修复回流达上限时无兜底终态，
    graph 在 KeyError 或空转中失败。
    """

    graph = workflow.ReactLikeWorkflow()._build_graph(checkpointer=InMemorySaver())
    graph_repr = graph.get_graph()
    node_names = set(graph_repr.nodes)

    assert {"model", "tools", "observe", "max_steps"} <= node_names
    targets = {
        edge.target
        for edge in graph_repr.edges
        if edge.source == "model"
    }
    assert "max_steps" in targets
    assert "model" in targets
    assert "tools" in targets
    assert END in {edge.target for edge in graph_repr.edges if edge.source == "max_steps"}


# =============================================================================
# 测试点：_tools_node 执行前取消分支的配对闭合（审查回归）
# =============================================================================


async def test_tools_node_cancel_before_execution_closes_pairing(
    monkeypatch,
) -> None:
    """测试目的：审批恢复后、执行前检测到 turn 已取消时，_tools_node 必须仍为本轮已写出的
    tool_calls 补同构占位 ToolMessage，闭合上一轮 _model_node 落库的 AIMessage.tool_calls 配对。

    可能发现的缺陷：取消分支提前 return 未补占位，导致下一轮模型请求因悬空 tool_calls 触发
    OpenAI 协议校验失败；或占位协议字段与 ToolExecutionService 内部序列化不一致。
    """
    from app.core.workflows.nodes import tools_node as tools_node_mod

    # 取消信号：operations.is_current_turn_cancelled 返回 True。
    operations = MagicMock()
    operations.is_current_turn_cancelled.return_value = True

    # 复用 service 公开门面：build_cancel_placeholder_messages 根据传入的 ToolCall
    # 返回带对应 tool_call_id 的 RuntimeMessage 占位列表（模拟 service 同源序列化）。
    def _fake_build_placeholders(calls: list[ToolCall]) -> list[RuntimeMessage]:
        return [
            RuntimeMessage(
                role="tool",
                content_text='{"content": "cancelled"}',
                metadata={"tool_call_id": call.call_id},
            )
            for call in calls
        ]

    operations.build_cancel_placeholder_messages.side_effect = _fake_build_placeholders

    # RuntimeConfig 持有取消中的 operations。
    rc = MagicMock()
    rc.operations = operations
    rc.approval_resolver = None

    # 隔离 graph 运行上下文依赖：interrupt 直接放行、context/config 返回测试替身、
    # 事件写入与观察落库可断言。
    monkeypatch.setattr(tools_node_mod, "interrupt", lambda _: None)
    monkeypatch.setattr(tools_node_mod, "_runtime_config", lambda: rc)
    monkeypatch.setattr(tools_node_mod, "_runtime_context", lambda: _FakeRuntimeContext())

    written_events: list[tuple[EventType, Any]] = []

    def _fake_write_event(event_type: EventType, payload: Any) -> None:
        written_events.append((event_type, payload))

    monkeypatch.setattr(
        tools_node_mod, "_make_write_event", lambda: _fake_write_event
    )

    persisted: list[RuntimeMessage] = []
    monkeypatch.setattr(
        tools_node_mod,
        "_persist_tool_observations",
        lambda _ops, msgs: persisted.extend(msgs),
    )

    state = ReactGraphState(
        step_count=2,
        tool_error_count=0,
        requested_tool=True,
        repair_requested="false",
        continuation_error_data=None,
        final_response=False,
        terminal=False,
        pending_tool_calls=[
            {"call_id": "call_a", "tool_name": "read_file"},
            {"call_id": "call_b", "tool_name": "write_file"},
        ],
        max_steps=5,
        final_text="",
        last_tool_results=[],
    )

    result = await tools_node_mod._tools_node(state)

    # 占位被写回，覆盖上一轮全部 tool_calls（按 call_id 配对闭合）。
    persisted_ids = {msg.metadata.get("tool_call_id") for msg in persisted}
    assert persisted_ids == {"call_a", "call_b"}
    # graph 走 END，不进 observe（占位由本分支自行补，不依赖 run_tool_calls）。
    assert result["terminal"] is True
    assert result["pending_tool_calls"] == []
    # 取消终态事件已发，供前端 StatusBadge 渲染。
    assert any(ev[0] == EventType.RUN_CANCELLED for ev in written_events)
    # run_tool_calls 未被调用（取消分支提前 return）。
    operations.run_tool_calls.assert_not_called()

