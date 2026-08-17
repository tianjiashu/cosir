"""``_model_node`` 的 invalid_tool_calls 双轨消费与失败路径测试（Task 3 测试点 5-9）。

字段口径按当前契约：``repair_requested`` 恒为 **str**（``"true"`` / ``"false"``），
``model_repair_count`` 已废弃不得出现；情形 b（无合法工具 + 命中工具名）必须非终态回流。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessageChunk, SystemMessage

from app.core.workflows.nodes import model_node
from app.core.workflows.nodes.model_tool_helper import InvalidToolOutcome
from app.core.workflows.react.runtime_config import RuntimeConfig
from app.core.workflows.react.state import ReactGraphState
from app.models.enums.event_type import EventType
from app.models.turn_usage_stats import TurnUsageStats


def _make_runtime_config(model: AsyncMock, tool_names: tuple[str, ...] = ()) -> RuntimeConfig:
    """构造供测试的最小 ``RuntimeConfig``（operations / turn 用 mock）。

    参数:
        model: 假 chat model（测试内替换 ``astream``）。
        tool_names: 注册给模型的工具名集合，决定 invalid_tool_call 能否命中。

    返回:
        字段完整的 ``RuntimeConfig``。

    异常:
        无。

    副作用:
        无。
    """

    operations = MagicMock()
    operations.is_current_turn_cancelled.return_value = False
    operations.fail_turn_if_running.return_value = MagicMock()
    operations.complete_turn_if_running.return_value = MagicMock()
    tools = []
    for name in tool_names:
        tool = MagicMock()
        tool.name = name
        tools.append(tool)
    operations.model_tools = tools
    turn = MagicMock()
    turn.turn_id = "turn_test"
    return RuntimeConfig(
        operations=operations,
        turn=turn,
        model=model,
        usage_stats=TurnUsageStats(),
        langfuse_trace_id="trace_test",
    )


def _make_state(max_steps: int = 10, step_count: int = 0) -> ReactGraphState:
    """构造最小 graph state（str 口径的 ``repair_requested``）。

    参数:
        max_steps: 步数上限。
        step_count: 当前步数。

    返回:
        字段完整的 ``ReactGraphState``。

    异常:
        无。

    副作用:
        无。
    """

    return ReactGraphState(
        step_count=step_count,
        max_steps=max_steps,
        tool_error_count=0,
        requested_tool=False,
        repair_requested="false",
        final_response=False,
        terminal=False,
        pending_tool_calls=[],
        final_text="",
        last_tool_results=[],
    )


def _install_runtime(
    monkeypatch,
    rc: RuntimeConfig,
    runtime_context: MagicMock,
) -> list[tuple[EventType, object]]:
    """把 ``_model_node`` 依赖的运行时原语替换成测试替身并捕获事件。

    参数:
        monkeypatch: pytest 注入的 monkeypatch fixture。
        rc: 测试用运行时配置。
        runtime_context: 假 RuntimeContext（捕获 ``add_message``）。

    返回:
        事件捕获列表（``(event_type, payload)``）。

    异常:
        无。

    副作用:
        替换 ``model_node`` 的 ``_runtime_config`` / ``_runtime_context`` / ``write_event``。
    """

    monkeypatch.setattr(model_node, "_runtime_config", lambda: rc)
    monkeypatch.setattr(model_node, "_runtime_context", lambda: runtime_context)
    events: list[tuple[EventType, object]] = []
    monkeypatch.setattr(
        model_node,
        "write_event",
        lambda event_type, payload: events.append((event_type, payload)),
    )
    return events


def _make_runtime_context() -> MagicMock:
    """构造捕获 ``add_message`` 的假 RuntimeContext。

    参数:
        无。

    返回:
        ``load_message`` 返回空列表、``add_message`` 可断言的 MagicMock。

    异常:
        无。

    副作用:
        无。
    """

    runtime_context = MagicMock()
    runtime_context.load_message.return_value = []
    runtime_context.add_message.return_value = None
    return runtime_context


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
        id="lc_run--test",
        tool_calls=tool_calls or [],
        invalid_tool_calls=invalid_tool_calls or [],
        response_metadata={"finish_reason": "tool_calls", "model_name": "deepseek-v4-flash"},
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_token_details": {"cache_read": 3},
            "output_token_details": {"reasoning": 1},
        },
    )


def _install_stream(rc: RuntimeConfig, chunk: AIMessageChunk) -> None:
    """把 ``rc.model.astream`` 替换成只产出单个 chunk 的假流。

    参数:
        rc: 测试用运行时配置。
        chunk: 要产出的分块。

    返回:
        无。

    异常:
        无。

    副作用:
        改写 ``rc.model.astream``。
    """

    async def _fake_stream(_messages) -> AsyncIterator[AIMessageChunk]:
        yield chunk

    rc.model.astream = _fake_stream  # type: ignore[method-assign]


def _capture_warnings(monkeypatch) -> list[tuple[tuple, dict]]:
    """捕获 ``model_node`` 内的 warning 日志调用。

    参数:
        monkeypatch: pytest 注入的 monkeypatch fixture。

    返回:
        ``(args, kwargs)`` 元组列表。

    异常:
        无。

    副作用:
        替换 ``model_node.log.warning``。
    """

    warned: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        model_node.log,
        "warning",
        lambda *args, **kwargs: warned.append((args, kwargs)),
    )
    return warned


def _warning_events(warned: list[tuple[tuple, dict]]) -> list[str]:
    """抽取 warning 的事件名列表。

    参数:
        warned: ``_capture_warnings`` 收集的调用列表。

    返回:
        事件名字符串列表。

    异常:
        无。

    副作用:
        无。
    """

    return [str(args[0]) for args, _kwargs in warned if args]


# =============================================================================
# 测试点 5：IGNORE 只 warning，不回流、不阻塞
# =============================================================================


async def test_ignore_only_warns(monkeypatch) -> None:
    """测试目的：IGNORE 非空 + REPAIR 空时，只打 warning，合法工具照常执行、不回流。

    可能发现的缺陷：噪声残片触发修复回流（虚假重试），或阻断合法工具执行；
    也可能 IGNORE warning 被误删（丢失排查线索）。
    """

    rc = _make_runtime_config(AsyncMock(), tool_names=("search_files",))
    runtime_context = _make_runtime_context()
    _install_runtime(monkeypatch, rc, runtime_context)
    _install_stream(
        rc,
        _make_chunk(
            tool_calls=[
                {"name": "search_files", "args": {}, "id": "call_1", "type": "tool_call"}
            ],
            invalid_tool_calls=[{"name": "", "args": '"', "id": "", "type": "tool_call"}],
        ),
    )
    warned = _capture_warnings(monkeypatch)

    result = await model_node._model_node(_make_state())

    assert "model_node_invalid_tool_calls_ignored" in _warning_events(warned)
    # 不回流、不阻塞：走工具分支，非终态
    assert result["repair_requested"] == "false"
    assert result["requested_tool"] is True
    assert result["terminal"] is False
    assert result["pending_tool_calls"] == [
        {
            "tool_name": "search_files",
            "arguments": {},
            "call_id": "call_1",
            "instruction": "",
        }
    ]
    # IGNORE 轨不产出修复提示（无 deferred_repair_message、无 continuation 明细）
    assert "deferred_repair_message" not in result["pending_tool_calls"][0]
    assert result["continuation_error_data"] is None


async def test_ignore_track_mocked_decision_does_not_trigger_repair(monkeypatch) -> None:
    """测试目的：直接 mock decide 返回「IGNORE 非空 + REPAIR 空」，验证双轨独立。

    可能发现的缺陷：执行分支不按决策结论分流，而是自行重算判定（决策/执行耦合）。
    """

    rc = _make_runtime_config(AsyncMock(), tool_names=("search_files",))
    runtime_context = _make_runtime_context()
    _install_runtime(monkeypatch, rc, runtime_context)
    _install_stream(
        rc,
        _make_chunk(
            content="",
            tool_calls=[
                {"name": "search_files", "args": {}, "id": "call_1", "type": "tool_call"}
            ],
            invalid_tool_calls=[{"name": "search_files", "args": "{", "id": "bad"}],
        ),
    )
    monkeypatch.setattr(
        model_node.ModelToolHelper,
        "decide_invalid_tool_handling",
        staticmethod(
            lambda **_kwargs: {
                InvalidToolOutcome.IGNORE: [{"name": "noise"}],
                InvalidToolOutcome.REPAIR: [],
            }
        ),
    )
    warned = _capture_warnings(monkeypatch)

    result = await model_node._model_node(_make_state())

    events = _warning_events(warned)
    assert "model_node_invalid_tool_calls_ignored" in events
    assert "model_node_invalid_tool_calls_deferred_repair_requested" not in events
    assert "model_node_invalid_tool_calls_no_tool_deferred" not in events
    assert result["repair_requested"] == "false"
    assert result["requested_tool"] is True
    assert "deferred_repair_message" not in result["pending_tool_calls"][0]


# =============================================================================
# 测试点 6：REPAIR + requested_tool=True（情形 a）
# =============================================================================


async def test_repair_with_requested_tool_defers(monkeypatch) -> None:
    """测试目的：情形 a 下合法工具照常执行，修复提示经 ``deferred_repair_message``
    延后到 tools 节点写上下文（用 fake runtime_context 捕获 add_message 断言 str 内容）。

    可能发现的缺陷：REPAIR 分支直接 ``return terminal_state`` 堵死工具执行
    （plan §0.1 #3），或 ``repair_message.content`` 类型 bug 使提示恒为空（#4）。
    """

    rc = _make_runtime_config(AsyncMock(), tool_names=("delegate_task", "search_files"))
    runtime_context = _make_runtime_context()
    _install_runtime(monkeypatch, rc, runtime_context)
    _install_stream(
        rc,
        _make_chunk(
            content="我先查一下文件",
            tool_calls=[
                {"name": "search_files", "args": {}, "id": "call_1", "type": "tool_call"}
            ],
            invalid_tool_calls=[
                {
                    "name": "delegate_task",
                    "args": '{"child_agent_id":"analyst",',
                    "id": "call_bad",
                    "type": "tool_call",
                    "error": "Expecting property name enclosed in double quotes",
                }
            ],
        ),
    )
    warned = _capture_warnings(monkeypatch)

    result = await model_node._model_node(_make_state())

    assert "model_node_invalid_tool_calls_deferred_repair_requested" in _warning_events(warned)
    assert result["terminal"] is False
    assert result["requested_tool"] is True
    assert result["repair_requested"] == "false"

    pending = result["pending_tool_calls"][0]
    assert pending["tool_name"] == "search_files"
    assert pending["instruction"] == "我先查一下文件"
    deferred = pending["deferred_repair_message"]
    assert isinstance(deferred, str)
    # 类型 bug 反例：若实现取 .content 会得到空串
    assert deferred, "deferred_repair_message 不得为空（str(repair_message) 契约）"
    assert "delegate_task" in deferred
    assert "NOT executed" in deferred

    # 情形 a 不直接写 RuntimeContext 修复提示（由 tools_node 在工具观察后写入）
    system_messages = [
        call.args[0]
        for call in runtime_context.add_message.call_args_list
        if call.args and isinstance(call.args[0], SystemMessage)
    ]
    assert system_messages == [], "情形 a 的修复提示必须延后到 tools_node 写入"
    # 兜底明细形状与 max_steps_node 消费口径对齐（且不含未脱敏原文）
    assert result["continuation_error_data"] == {
        "error_kind": "invalid_tool_call_repair",
        "invalid_count": 1,
    }


async def test_repair_with_valid_tool_marks_every_pending_call(monkeypatch) -> None:
    """测试目的：情形 a 多个合法工具调用时每条都带上 ``deferred_repair_message``。

    可能发现的缺陷：只给首条打标 → tools_node 的 ``next(...)`` 取值依赖顺序，
    审批裁剪掉首条后修复提示丢失。
    """

    rc = _make_runtime_config(AsyncMock(), tool_names=("delegate_task", "search_files"))
    runtime_context = _make_runtime_context()
    _install_runtime(monkeypatch, rc, runtime_context)
    _install_stream(
        rc,
        _make_chunk(
            tool_calls=[
                {"name": "search_files", "args": {}, "id": "call_1", "type": "tool_call"},
                {"name": "search_files", "args": {}, "id": "call_2", "type": "tool_call"},
            ],
            invalid_tool_calls=[
                {"name": "delegate_task", "args": "{", "id": "bad", "error": "bad json"}
            ],
        ),
    )

    result = await model_node._model_node(_make_state())

    assert len(result["pending_tool_calls"]) == 2
    for item in result["pending_tool_calls"]:
        assert "delegate_task" in item["deferred_repair_message"]


# =============================================================================
# 测试点 7：REPAIR + requested_tool=False（情形 b）
# =============================================================================


async def test_repair_without_requested_tool_returns_nonterminal(monkeypatch) -> None:
    """测试目的：情形 b 直接写 SystemMessage 到 RuntimeContext 并 return 非终态回流
    （``repair_requested=="true"``、``terminal==False``、未进入 invalid_output 终态）。

    可能发现的缺陷：情形 b 缺失 → 滑落到 ``invalid_model_output`` 终态被误判失败
    （plan §0.1 #6），或 ``repair_requested`` 未置 ``"true"`` 使 graph 直接 END（#11）。
    """

    rc = _make_runtime_config(AsyncMock(), tool_names=("delegate_task",))
    runtime_context = _make_runtime_context()
    events = _install_runtime(monkeypatch, rc, runtime_context)
    _install_stream(
        rc,
        _make_chunk(
            content="我会启动两个子 Agent。",
            tool_calls=[],
            invalid_tool_calls=[
                {
                    "name": "delegate_task",
                    "args": '{"child_agent_id":"delegate_analyst",',
                    "id": "call_bad",
                    "type": "tool_call",
                    "error": "Expecting property name enclosed in double quotes",
                }
            ],
        ),
    )
    warned = _capture_warnings(monkeypatch)

    result = await model_node._model_node(_make_state())

    assert "model_node_invalid_tool_calls_no_tool_deferred" in _warning_events(warned)
    # 非终态回流契约
    assert result["terminal"] is False
    assert result["repair_requested"] == "true"
    assert result["requested_tool"] is False
    assert result["final_response"] is False
    assert result["pending_tool_calls"] == []
    assert result["step_count"] == 1

    # 修复提示直接写进 RuntimeContext（捕获的 add_message 必须是 str 内容）
    system_calls = [
        call.args[0]
        for call in runtime_context.add_message.call_args_list
        if call.args and isinstance(call.args[0], SystemMessage)
    ]
    assert len(system_calls) == 1
    content = str(system_calls[0].content)
    assert "delegate_task" in content
    assert "NOT executed" in content

    # 非终态路径不得发终态事件（终态由 max_steps 兜底）
    assert [et for et, _p in events if et == EventType.RUN_FAILED] == []
    assert [et for et, _p in events if et == EventType.FINAL_RESPONSE] == []
    assert [et for et, _p in events if et == EventType.RUN_FINISHED] == []


async def test_repair_without_valid_tool_does_not_slide_to_invalid_output(monkeypatch) -> None:
    """测试目的：情形 b 且**无任何文本**时也不落 ``invalid_model_output`` 终态。

    可能发现的缺陷：return 位置排在 ``if output_text`` / invalid_output 之后 →
    无文本的可疑调用被误判非法输出，turn 被 fail 掉（plan 修复点 #1 的核心）。
    """

    rc = _make_runtime_config(AsyncMock(), tool_names=("delegate_task",))
    runtime_context = _make_runtime_context()
    events = _install_runtime(monkeypatch, rc, runtime_context)
    _install_stream(
        rc,
        _make_chunk(
            content="",
            tool_calls=[],
            invalid_tool_calls=[
                {"name": "delegate_task", "args": "{", "id": "bad", "error": "bad json"}
            ],
        ),
    )

    result = await model_node._model_node(_make_state())

    assert result["terminal"] is False
    assert result["repair_requested"] == "true"
    assert [et for et, _p in events if et == EventType.RUN_FAILED] == []
    rc.operations.fail_turn_if_running.assert_not_called()


async def test_mixed_ignore_and_repair_without_tool_reflows_independently(monkeypatch) -> None:
    """测试目的：混合列表（IGNORE + REPAIR）且无合法工具时，两轨独立生效。

    可能发现的缺陷：IGNORE 的存在抑制 REPAIR 回流（双轨耦合）→ 噪声项一旦同时出现
    自愈就失效，并滑落 invalid_output 终态（plan §6.2 混合场景）。
    """

    rc = _make_runtime_config(AsyncMock(), tool_names=("delegate_task",))
    runtime_context = _make_runtime_context()
    events = _install_runtime(monkeypatch, rc, runtime_context)
    _install_stream(
        rc,
        _make_chunk(
            content="",
            tool_calls=[],
            invalid_tool_calls=[
                {"name": "", "args": "%%%", "id": "noise", "error": "unparsable"},
                {"name": "delegate_task", "args": "{", "id": "bad", "error": "bad json"},
            ],
        ),
    )
    warned = _capture_warnings(monkeypatch)

    result = await model_node._model_node(_make_state())

    warning_events = _warning_events(warned)
    assert "model_node_invalid_tool_calls_ignored" in warning_events
    assert "model_node_invalid_tool_calls_no_tool_deferred" in warning_events
    assert result["terminal"] is False
    assert result["repair_requested"] == "true"
    assert [et for et, _p in events if et == EventType.RUN_FAILED] == []
    system_messages = [
        call.args[0]
        for call in runtime_context.add_message.call_args_list
        if call.args and isinstance(call.args[0], SystemMessage)
    ]
    assert len(system_messages) == 1
    assert "delegate_task" in str(system_messages[0].content)


async def test_no_repair_and_no_tool_falls_to_invalid_model_output(monkeypatch) -> None:
    """测试目的：无合法工具 + 无命中（纯噪声、无文本）→ ``invalid_model_output`` 终态。

    可能发现的缺陷：错误分类被编造为 ``parse_invalid``（全库无此分类），或漏发终态
    事件导致前端永远等不到结束信号。
    """

    rc = _make_runtime_config(AsyncMock(), tool_names=("delegate_task",))
    runtime_context = _make_runtime_context()
    events = _install_runtime(monkeypatch, rc, runtime_context)
    _install_stream(
        rc,
        _make_chunk(
            content="",
            tool_calls=[],
            invalid_tool_calls=[{"name": "", "args": "%%%", "id": "noise"}],
        ),
    )

    result = await model_node._model_node(_make_state())

    assert result["terminal"] is True
    assert result["repair_requested"] == "false"
    failed = [payload for et, payload in events if et == EventType.RUN_FAILED]
    assert failed
    assert failed[0].error == "invalid_model_output"
    rc.operations.fail_turn_if_running.assert_called_once_with(
        "turn_test", end_reason="invalid_model_output"
    )


async def test_empty_invalid_tool_calls_triggers_no_repair_branch(monkeypatch) -> None:
    """测试目的：边界——无 invalid_tool_calls 时完全不进修复分支。

    可能发现的缺陷：空列表触发空提示写入 RuntimeContext，或写出多余 warning。
    """

    rc = _make_runtime_config(AsyncMock(), tool_names=("search_files",))
    runtime_context = _make_runtime_context()
    _install_runtime(monkeypatch, rc, runtime_context)
    _install_stream(
        rc,
        _make_chunk(
            tool_calls=[
                {"name": "search_files", "args": {}, "id": "call_1", "type": "tool_call"}
            ],
            invalid_tool_calls=[],
        ),
    )
    warned = _capture_warnings(monkeypatch)

    result = await model_node._model_node(_make_state())

    assert warned == []
    assert result["repair_requested"] == "false"
    assert result["continuation_error_data"] is None
    assert "deferred_repair_message" not in result["pending_tool_calls"][0]
    system_messages = [
        call.args[0]
        for call in runtime_context.add_message.call_args_list
        if call.args and isinstance(call.args[0], SystemMessage)
    ]
    assert system_messages == []


# =============================================================================
# 测试点 8 / 9：repair_requested 全 str、model_repair_count 不再出现
# =============================================================================


async def _run_and_collect_patches(monkeypatch) -> list[dict[str, Any]]:
    """跑完 ``_model_node`` 的各主要分支，收集所有 return patch。

    参数:
        monkeypatch: pytest 注入的 monkeypatch fixture。

    返回:
        各分支返回的 state patch 列表（含情形 a / 情形 b / 终态 / 取消 / 最终回复）。

    异常:
        无。

    副作用:
        逐分支替换运行时替身与 astream。
    """

    patches: list[dict[str, Any]] = []

    # 情形 a：合法工具 + REPAIR
    rc = _make_runtime_config(AsyncMock(), tool_names=("delegate_task", "search_files"))
    _install_runtime(monkeypatch, rc, _make_runtime_context())
    _install_stream(
        rc,
        _make_chunk(
            tool_calls=[
                {"name": "search_files", "args": {}, "id": "call_1", "type": "tool_call"}
            ],
            invalid_tool_calls=[{"name": "delegate_task", "args": "{", "id": "bad"}],
        ),
    )
    patches.append(await model_node._model_node(_make_state()))

    # 情形 b：无合法工具 + REPAIR
    rc = _make_runtime_config(AsyncMock(), tool_names=("delegate_task",))
    _install_runtime(monkeypatch, rc, _make_runtime_context())
    _install_stream(
        rc,
        _make_chunk(
            invalid_tool_calls=[{"name": "delegate_task", "args": "{", "id": "bad"}]
        ),
    )
    patches.append(await model_node._model_node(_make_state()))

    # 非法输出终态
    rc = _make_runtime_config(AsyncMock(), tool_names=("delegate_task",))
    _install_runtime(monkeypatch, rc, _make_runtime_context())
    _install_stream(rc, _make_chunk())
    patches.append(await model_node._model_node(_make_state()))

    # 最终回复终态
    rc = _make_runtime_config(AsyncMock(), tool_names=("delegate_task",))
    _install_runtime(monkeypatch, rc, _make_runtime_context())
    _install_stream(rc, _make_chunk(content="最终答案"))
    patches.append(await model_node._model_node(_make_state()))

    # 请求前取消
    rc = _make_runtime_config(AsyncMock(), tool_names=("delegate_task",))
    rc.operations.is_current_turn_cancelled.return_value = True
    _install_runtime(monkeypatch, rc, _make_runtime_context())
    _install_stream(rc, _make_chunk())
    patches.append(await model_node._model_node(_make_state()))

    return patches


async def test_repair_requested_is_str(monkeypatch) -> None:
    """测试目的：所有 return patch 的 ``repair_requested`` 均为 str（"false"/"true"），
    无 bool 残留。

    可能发现的缺陷：残留 ``"repair_requested": False``（bool）与 ``state.py`` 的 str
    声明冲突 → pydantic 校验失败或路由判定口径漂移（plan §0.1 #11）。
    """

    patches = await _run_and_collect_patches(monkeypatch)

    assert len(patches) == 5
    for patch in patches:
        # 契约：必须是 "true"/"false" 字符串，不得是 bool（与 state.py 的 str 声明一致）
        value = patch["repair_requested"]
        assert value in {"true", "false"}, (
            "repair_requested 必须是 str 字面量 'true'/'false'，"
            f"实际 {type(value).__name__}={value!r}"
        )


async def test_no_model_repair_count(monkeypatch) -> None:
    """测试目的：``model_repair_count`` 死写已删除，不出现在任何 return patch。

    可能发现的缺陷：残留未声明字段写入 → strict schema 校验回归时报错
    （plan 修复点 #7）。
    """

    patches = await _run_and_collect_patches(monkeypatch)

    for patch in patches:
        assert "model_repair_count" not in patch


async def test_returned_patch_fields_are_accepted_by_state_schema(monkeypatch) -> None:
    """测试目的：情形 b 的 patch 字段可被 ``ReactGraphState`` 直接接受（schema 完整）。

    可能发现的缺陷：patch 写入 state 未声明的字段（如 ``continuation_error_data``
    缺字段定义）→ strict 校验下回流路径崩溃。
    """

    rc = _make_runtime_config(AsyncMock(), tool_names=("delegate_task",))
    _install_runtime(monkeypatch, rc, _make_runtime_context())
    _install_stream(
        rc,
        _make_chunk(
            invalid_tool_calls=[{"name": "delegate_task", "args": "{", "id": "bad"}]
        ),
    )

    patch = await model_node._model_node(_make_state())
    merged = _make_state().model_copy(update=patch)

    assert merged.repair_requested == "true"
    assert merged.terminal is False
    assert merged.continuation_error_data is None


# =============================================================================
# 既有失败路径回归（usage 摘要 / 取消 / 步数上限交给条件边）
# =============================================================================


async def test_run_failed_carries_usage_summary(monkeypatch) -> None:
    """测试目的：非法输出终态的 RUN_FAILED 必须携带本轮 token 摘要。

    可能发现的缺陷：失败事件漏 usage 字段 → 前端 token 统计缺失、成本不可排查。
    """

    rc = _make_runtime_config(AsyncMock())
    events = _install_runtime(monkeypatch, rc, _make_runtime_context())
    _install_stream(
        rc,
        AIMessageChunk(
            content="",
            id="lc_run--empty",
            tool_calls=[],
            invalid_tool_calls=[],
            response_metadata={"finish_reason": "stop"},
            usage_metadata={
                "input_tokens": 7,
                "output_tokens": 2,
                "total_tokens": 9,
                "input_token_details": {"cache_read": 1},
                "output_token_details": {"reasoning": 0},
            },
        ),
    )

    result = await model_node._model_node(_make_state())

    assert result["terminal"] is True
    failed = [payload for et, payload in events if et == EventType.RUN_FAILED]
    assert failed
    payload = failed[0]
    assert (payload.input_tokens, payload.output_tokens, payload.total_tokens) == (7, 2, 9)


async def test_cancellation_emits_run_cancelled_with_usage(monkeypatch) -> None:
    """测试目的：取消分支发 RUN_CANCELLED 并记录已消耗 token 摘要。

    可能发现的缺陷：取消时静默丢弃 usage，或错误发 RUN_FAILED 造成状态机误判。
    """

    rc = _make_runtime_config(AsyncMock())
    events = _install_runtime(monkeypatch, rc, _make_runtime_context())
    chunk = _make_chunk("部分输出")

    async def _fake_stream(_messages) -> AsyncIterator[AIMessageChunk]:
        rc.operations.is_current_turn_cancelled.return_value = True
        yield chunk

    rc.model.astream = _fake_stream  # type: ignore[method-assign]
    warned = _capture_warnings(monkeypatch)

    result = await model_node._model_node(_make_state())

    assert result["terminal"] is True
    assert result["repair_requested"] == "false"
    assert "model_node_cancelled_usage_summary" in _warning_events(warned)
    cancelled = [payload for et, payload in events if et == EventType.RUN_CANCELLED]
    assert cancelled
    assert cancelled[0].status == "cancelled"
    assert cancelled[0].total_tokens == 15
    assert [et for et, _p in events if et == EventType.RUN_FAILED] == []


async def test_tool_request_at_step_limit_defers_to_edge_gate(monkeypatch) -> None:
    """测试目的：达 ``max_steps`` 时 model 节点只标记工具意图，不自行终态。

    可能发现的缺陷：节点内重复实现步数拦截 → 与条件边双重终态、事件重复。
    """

    rc = _make_runtime_config(AsyncMock(), tool_names=("search_files",))
    events = _install_runtime(monkeypatch, rc, _make_runtime_context())
    _install_stream(
        rc,
        _make_chunk(
            tool_calls=[
                {"name": "search_files", "args": {}, "id": "call_1", "type": "tool_call"}
            ]
        ),
    )

    state = _make_state(max_steps=1, step_count=1)
    result = await model_node._model_node(state)

    assert result["terminal"] is False
    assert result["requested_tool"] is True
    assert result["repair_requested"] == "false"
    assert result["continuation_error_data"] is None
    assert [et for et, _p in events if et == EventType.RUN_FAILED] == []
