"""ReAct-like 工作流「协作取消」的路由与暂停语义回归测试。

覆盖三类事实（这些是「取消不结束图、可续跑」这一设计的核心断言）：

1. **路由**：``cancel_requested`` 分别把 ``model`` / ``tools`` 出口导向 ``observe``，
   把 ``observe`` 出口导向 ``pause``；``pause`` 恢复后交回 ``model``；真实终态仍走 ``END``。
2. **暂停语义**：取消经 ``observe`` 收口后图停在 ``pause``（``next`` 非空、``terminal`` 为假），
   因此该 checkpoint 仍可续跑。
3. **续跑准入判据**：图走到 ``END`` 后 ``snapshot.next`` 为空——这是
   ``ReactLikeWorkflow.run`` 拒绝续跑并收敛 run 的判据（原缺陷：此时仍进入 ``astream``
   会永久挂起）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.core.workflows.nodes.pause_node import _pause_node
from app.core.workflows.react.edges import (
    _after_observe,
    _after_pause,
    _after_tools,
    _should_continue,
)
from app.core.workflows.react.state import ReactGraphState


def _state(**overrides: Any) -> ReactGraphState:
    """构造一个合法的 state，供纯函数路由断言使用。"""

    base: dict[str, Any] = {
        "step_count": 1,
        "tool_error_count": 0,
        "requested_tool": False,
        "final_response": False,
        "terminal": False,
        "instruction": "",
        "max_steps": 10,
        "final_text": "",
        "last_tool_results": {},
        "continue_model": False,
        "cancel_requested": False,
        "tool_call_lifecycle": None,
        "continuation_error_data": None,
    }
    base.update(overrides)
    return ReactGraphState(**base)


# --------------------------------------------------------------------------- 路由


def test_should_continue_routes_cancel_to_observe() -> None:
    """取消优先于工具分支：即使模型请求了工具，也要先去 observe 收口取消。"""

    assert _should_continue(_state(cancel_requested=True, requested_tool=True)) == "observe"


def test_should_continue_keeps_normal_routing() -> None:
    """无取消时既有路由不变：工具 / 继续推理 / 最终回答。"""

    assert _should_continue(_state(requested_tool=True)) == "tools"
    assert _should_continue(_state(continue_model=True)) == "model"
    assert _should_continue(_state(final_response=True)) == END
    assert _should_continue(_state(terminal=True)) == END


def test_after_tools_enters_observe_unless_terminal() -> None:
    """工具出口不区分取消（与正常执行同路）；只有真实终态才 END。"""

    assert _after_tools(_state(cancel_requested=True)) == "observe"
    assert _after_tools(_state()) == "observe"
    assert _after_tools(_state(terminal=True)) == END


def test_after_observe_routes_cancel_to_pause() -> None:
    """取消经 observe 收口后必须停在 pause（而不是 END），否则无法续跑。"""

    assert _after_observe(_state(cancel_requested=True)) == "pause"
    assert _after_observe(_state(cancel_requested=True, terminal=True)) == "pause"


def test_after_observe_keeps_normal_routing() -> None:
    """无取消时既是回流 model，也是达错误上限 END。"""

    assert _after_observe(_state()) == "model"
    assert _after_observe(_state(terminal=True)) == END
    assert _after_observe(_state(final_response=True)) == END


def test_after_pause_returns_to_model_and_has_end_fallback() -> None:
    """恢复后回 model；取消标志若未清则 END 兜底（防 observe↔pause 死循环）。"""

    assert _after_pause(_state()) == "model"
    assert _after_pause(_state(cancel_requested=True)) == END


# ------------------------------------------------------------------- 暂停与续跑语义


def _build_cancel_graph(calls: list[str] | None = None) -> Any:
    """用真实 edges 函数 + stub 节点搭一张最小图，复现「取消 ⇒ observe ⇒ pause」。

    参数:
        calls: 可选执行轨迹收集器；model / observe 节点被真正执行时追加自己的名字，
            用于断言「恢复后是否从 model 重新执行」。
    """

    async def _model_node(state: ReactGraphState) -> dict:
        """首次进入模拟流式中观察到取消；续跑后再进入则正常结束图。"""

        if calls is not None:
            calls.append("model")
        next_step = state.step_count + 1
        if state.step_count == 1:
            return {"step_count": next_step, "cancel_requested": True}
        return {"step_count": next_step, "terminal": True}

    async def _observe_node(state: ReactGraphState) -> dict:
        """模拟观察节点作为取消的唯一收口点（此处只落路由标志）。"""

        if calls is not None:
            calls.append("observe")
        return {"cancel_requested": True}

    async def _never_node(state: ReactGraphState) -> dict:
        """取消路径不应到达此节点（到达即测试失败）。"""

        raise AssertionError("cancelled run must not reach model again before resume")

    builder = StateGraph(ReactGraphState)
    builder.add_node("model", _model_node)
    builder.add_node("tools", _never_node)
    builder.add_node("observe", _observe_node)
    builder.add_node("pause", _pause_node)
    builder.add_edge(START, "model")
    builder.add_conditional_edges(
        "model",
        _should_continue,
        {"tools": "tools", "model": "model", "observe": "observe", END: END},
    )
    builder.add_conditional_edges("tools", _after_tools, {"observe": "observe", END: END})
    builder.add_conditional_edges(
        "observe", _after_observe, {"model": "model", "pause": "pause", END: END}
    )
    builder.add_conditional_edges("pause", _after_pause, {"model": "model", END: END})
    return builder.compile(checkpointer=InMemorySaver())


def _initial_state() -> ReactGraphState:
    return _state()


def test_cancel_stops_at_pause_and_keeps_checkpoint_resumable() -> None:
    """取消后图停在 pause：不 END、``next`` 非空 ⇒ 该 checkpoint 仍可续跑。"""

    async def _main() -> None:
        graph = _build_cancel_graph()
        config = {"configurable": {"thread_id": "cancel-pause"}}

        await graph.ainvoke(_initial_state(), config)

        snapshot = await graph.aget_state(config)
        assert snapshot.next == ("pause",), snapshot.next
        assert snapshot.values["cancel_requested"] is True
        assert snapshot.values["terminal"] is False
        # 关键：图未结束，因此「续跑」有可执行的节点。
        assert snapshot.next, "cancelled graph must not be finished"

    asyncio.run(_main())


def test_resume_from_pause_clears_flag_and_continues() -> None:
    """停在 pause 的图可用 ``Command(resume=...)`` 恢复，恢复后继续推进直至结束。"""

    async def _main() -> None:
        graph = _build_cancel_graph()
        config = {"configurable": {"thread_id": "resume-pause"}}
        await graph.ainvoke(_initial_state(), config)

        snapshot = await graph.aget_state(config)
        assert any(task.interrupts for task in snapshot.tasks), "pause 必须留下 interrupt"
        assert snapshot.next == ("pause",)

        # 与生产实现同构：恢复时传 ``Command(resume=...)``。
        await graph.ainvoke(Command(resume={"action": "resume"}), config)

        snapshot = await graph.aget_state(config)
        # 恢复后已离开 pause 并继续推进（本 stub 第二次进入 model 即终结图）。
        assert snapshot.next == ()
        assert snapshot.values["cancel_requested"] is False

    asyncio.run(_main())


def test_resume_reenters_model_node_after_pause() -> None:
    """恢复后确实**从 model 节点重新执行**（用户诉求：取消后恢复从 model 重跑）。

    轨迹断言 ``["model", "observe", "model"]``：最后一次进入的是 ``model``，
    证明 ``pause`` 恢复后经 ``_after_pause`` 回到 ``model``，而不是回到 ``observe``
    再次收口、也不是直接 END。
    """

    async def _main() -> None:
        calls: list[str] = []
        graph = _build_cancel_graph(calls)
        config = {"configurable": {"thread_id": "resume-model"}}

        await graph.ainvoke(_initial_state(), config)
        assert calls == ["model", "observe"]
        assert (await graph.aget_state(config)).next == ("pause",)

        await graph.ainvoke(Command(resume={"action": "resume"}), config)
        # 恢复后重新进入 model（第二个 "model" 即诉求中的「从 model 重新执行」）。
        assert calls == ["model", "observe", "model"]

    asyncio.run(_main())


def _cancel_ready_state(**overrides: Any) -> ReactGraphState:
    """模拟「尚未执行过任何工具步」的 state（``initial_state`` 的形态）。"""

    return _state(**overrides)


# ------------------------------------------------- 取消分支进入 observe 的契约


def test_model_precheck_cancel_patch_is_observe_ready() -> None:
    """model 请求前取消必须给出 observe 能直接消费的完整补丁。

    回归点：本分支经 ``_should_continue`` 进入 ``observe``，而该节点按
    ``last_tool_results`` 的键取值、并要求 ``tool_call_lifecycle`` 非 ``None``。首次运行
    （未执行过工具步）时二者仍是初始值 ⇒ 不随取消一起给出会 KeyError / RuntimeError。
    """

    async def _main() -> None:
        rc = SimpleNamespace(
            run=SimpleNamespace(id=9),
            operations=SimpleNamespace(
                get_current_run=lambda: SimpleNamespace(id=9, task_id=5),
                is_current_run_cancelled=lambda: True,
            ),
        )
        original = model_node_module._runtime_config
        model_node_module._runtime_config = lambda: rc
        try:
            patch = await model_node_module._model_node(_cancel_ready_state())
        finally:
            model_node_module._runtime_config = original

        assert patch["cancel_requested"] is True
        assert patch["step_count"] == 1
        assert patch["last_tool_results"] == {
            "instruction": "",
            "observations": [],
            "expected_call_ids": [],
        }
        assert patch["tool_call_lifecycle"] is not None

    asyncio.run(_main())


def test_observe_settles_cancel_from_empty_patch(monkeypatch: Any) -> None:
    """``observe`` 能在「空摘要 + 空 lifecycle」下完成取消收口与落库。

    回归点：``last_tool_results={}``（首次运行）与 ``tool_call_lifecycle=None`` 曾分别导致
    ``KeyError`` 与 ``RuntimeError``。
    """

    settled: dict[str, Any] = {}

    class _Ops:
        def get_current_task(self) -> Any:
            return SimpleNamespace(id=5)

        def get_current_run(self) -> Any:
            return SimpleNamespace(id=9)

        def cancel_run_if_running(self, **kwargs: Any) -> Any:
            settled.update(kwargs)
            return SimpleNamespace(id=9)

    rc = SimpleNamespace(operations=_Ops())
    monkeypatch.setattr(observation_node_module, "_runtime_config", lambda: rc)
    monkeypatch.setattr(
        observation_node_module,
        "_runtime_context",
        lambda: SimpleNamespace(load_message=lambda: []),
    )

    state = ReactGraphState(
        step_count=1,
        tool_error_count=0,
        requested_tool=False,
        final_response=False,
        terminal=False,
        instruction="",
        max_steps=10,
        final_text="",
        last_tool_results={},
        cancel_requested=True,
        tool_call_lifecycle=ToolCallLifecycleManager(),
    )
    patch = asyncio.run(observation_node_module._observe_node(state))

    assert patch["cancel_requested"] is True
    assert patch["tool_call_lifecycle"] is not None
    assert settled["end_reason"] == "runtime_cancelled"


def test_finished_graph_has_empty_next() -> None:
    """图走到 END 后 ``next`` 为空——这是 resume 必须拒绝的判据。"""

    async def _main() -> None:
        async def _final_node(state: ReactGraphState) -> dict:
            return {"terminal": True}

        builder = StateGraph(ReactGraphState)
        builder.add_node("model", _final_node)
        builder.add_edge(START, "model")
        builder.add_conditional_edges("model", _should_continue, {"observe": "observe", END: END})
        builder.add_node("observe", _final_node)
        builder.add_edge("observe", END)
        graph = builder.compile(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "finished"}}

        await graph.ainvoke(_initial_state(), config)

        snapshot = await graph.aget_state(config)
        assert snapshot.next == (), snapshot.next
        assert not snapshot.next, "已结束的图必须让 next 为空（resume 据此拒绝）"

    asyncio.run(_main())
