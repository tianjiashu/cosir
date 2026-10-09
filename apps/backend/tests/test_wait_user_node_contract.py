"""通用 human-in-the-loop 节点（``wait_user_node``）的派生、投影、挂起、物化与路由契约。

独立测试方：只验证节点自身的控制流（何时投影、何时挂起、恢复后去哪里、批准如何重开执行门控、
驳回如何改写观察、恢复值不完整时如何自环），不连接真实模型 / 数据库 / 工具执行层。

待决请求不由任何节点「登记」：它由观察上的 ``user_input_request`` 声明派生，因此这里直接构造
``last_tool_results``（形状与 ``tools`` 节点的 ``dataclasses.asdict`` 投影一致）与配套的生命周期
快照。续跑入口的两条语义在测试里显式模拟：恢复前把 ``resuming_wait_user`` 置为 True（真实链路
由 ``ReactLikeWorkflow.run`` 在识别到 ``wait_user`` 断点后置位），节点消费恢复值后应把它复位。
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.core.tools.schemas.user_decision import (
    UserDecision,
    UserDecisionKind,
    build_resume_payload,
)
from app.core.workflows.react.node_helper.tool_call_lifecycle import (
    ToolCallLifecycleManager,
    ToolCallLifecycleRecord,
)
from app.core.workflows.react.worflow_state.route import ReactRoute
from app.core.workflows.react.worflow_state.state import ReactGraphState

wait_node_module = importlib.import_module("app.core.workflows.react.nodes.wait_user_node")
lifecycle_module = importlib.import_module(
    "app.core.workflows.react.node_helper.tool_call_lifecycle"
)

_TOOL_NAME = "agent_team"

# 「未指定」哨兵：让 ``lifecycle=None`` 表达「显式缺失生命周期」而不触发默认夹具。
_UNSET: Any = object()


def _request(
    *,
    request_id: str = "12",
    decisions: tuple[str, ...] = ("approve", "reject"),
) -> dict[str, Any]:
    """构造待决请求的投影形状（与 ``dataclasses.asdict(UserInputRequest)`` 一致）。"""

    return {
        "kind": "agent_team_review",
        "request_id": request_id,
        "prompt": "确认执行方案",
        "decisions": list(decisions),
        "draft_schema": "agent-team-review-v1",
        "draft": {"goal": "交付报告"},
    }


def _observation(
    tool_call_id: str = "call-a",
    *,
    request_id: str = "12",
    request: object = _UNSET,
    decisions: tuple[str, ...] = ("approve", "reject"),
) -> dict[str, Any]:
    """构造一条工具观察摘要；``request=None`` 表示该调用没有待决声明（已作答或普通结果）。"""

    return {
        "tool_name": _TOOL_NAME,
        "status": "success",
        "content": '{"status": "pending"}',
        "error": "",
        "reason": "",
        "retryable": False,
        "tool_call_id": tool_call_id,
        "display_data": {"kind": "agent-team-preview", "status": "pending"},
        "user_input_request": (
            _request(request_id=request_id, decisions=decisions) if request is _UNSET else request
        ),
    }


def _lifecycle(
    *call_ids: str,
    allows: tuple[str, ...] = (_TOOL_NAME,),
    blocked: tuple[str, ...] = (),
) -> ToolCallLifecycleManager:
    """构造已起跑调用的生命周期快照：``valid_calls`` 为 running，``blocked_calls`` 为未放行。"""

    return ToolCallLifecycleManager(
        allows_tools=allows,
        valid_calls={
            call_id: ToolCallLifecycleRecord(
                tool_call_id=call_id, tool_name=_TOOL_NAME, status="running"
            )
            for call_id in call_ids
        },
        blocked_calls={
            call_id: ToolCallLifecycleRecord(
                tool_call_id=call_id,
                tool_name="write_file",
                status="pending",
                part_projected=False,
            )
            for call_id in blocked
        },
    )


def _state(
    observations: list[dict[str, Any]],
    *,
    lifecycle: Any = _UNSET,
) -> ReactGraphState:
    """构造等待节点所需的最小 graph state。"""

    return ReactGraphState(
        step_count=1,
        tool_error_count=0,
        next_node=ReactRoute.OBSERVE,
        max_steps=10,
        final_text="",
        last_tool_results={"instruction": "调用 Team", "observations": observations},
        tool_call_lifecycle=(
            _lifecycle("call-a", "call-b", "call-c") if lifecycle is _UNSET else lifecycle
        ),
    )


class _Harness:
    """承载最小图（``wait`` 自环 + ``tools`` / ``observe``）与运行时配置桩。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        self.migrations: list[str] = []
        self.projected: list[Any] = []
        # canonical context 写入（待确认占位行 / 真实结果行）：与真实契约同口径，同 tool_call_id
        # 命中既有行时原地覆盖，因此一条调用最终只有一行。
        self.context_writes: list[dict[str, Any]] = []
        self.runtime_config = SimpleNamespace(
            resuming_wait_user=False,
            run=SimpleNamespace(id=99),
            operations=SimpleNamespace(
                mark_waiting_for_input_if_running=self._mark_waiting,
                get_current_task=lambda: SimpleNamespace(id=42),
            ),
        )
        self.runtime_context = SimpleNamespace(add_message=self._add_message)
        monkeypatch.setattr(wait_node_module, "_runtime_config", lambda: self.runtime_config)
        # 投影经 lifecycle 的状态事件走到 stream writer；这里直接收集，以断言「何时投影」。
        monkeypatch.setattr(lifecycle_module, "get_stream_writer", lambda: self.projected.append)
        monkeypatch.setattr(lifecycle_module, "_runtime_context", lambda: self.runtime_context)
        self.config = {"configurable": {"thread_id": f"wait-user-{id(self)}"}}
        self.graph = self._build()

    def _mark_waiting(self) -> SimpleNamespace:
        """按真实契约返回迁移后的 Run 记录（返回 None 表示条件更新未命中）。"""

        self.migrations.append("mark")
        return SimpleNamespace(id=99, status="waiting_for_input")

    def _add_message(self, message: Any, **kwargs: Any) -> str:
        """模拟 canonical context 写入；同 ``tool_call_id`` 命中既有行即原地覆盖。"""

        call_id = getattr(message, "tool_call_id", None) or ""
        for index, existing in enumerate(self.context_writes):
            if getattr(existing["message"], "tool_call_id", None) == call_id:
                self.context_writes[index] = {"message": message, **kwargs}
                return "replaced"
        self.context_writes.append({"message": message, **kwargs})
        return "appended"

    def waiting_rows(self) -> dict[str, dict[str, Any]]:
        """取持久化下来的待确认占位行，键为 ``tool_call_id``。"""

        return {
            str(getattr(row["message"], "tool_call_id", "") or ""): row
            for row in self.context_writes
        }

    def _build(self) -> Any:
        builder = StateGraph(ReactGraphState)

        def _tools(_state: ReactGraphState) -> dict[str, Any]:
            self.calls.append("tools")
            return {"next_node": ReactRoute.OBSERVE}

        def _observe(_state: ReactGraphState) -> dict[str, Any]:
            self.calls.append("observe")
            return {"next_node": ReactRoute.END}

        builder.add_node("wait", wait_node_module.wait_user_node)
        builder.add_node("tools", _tools)
        builder.add_node("observe", _observe)
        builder.add_edge(START, "wait")
        builder.add_conditional_edges(
            "wait",
            lambda state: state.next_node.value,
            {
                ReactRoute.TOOLS.value: "tools",
                ReactRoute.OBSERVE.value: "observe",
                ReactRoute.WAIT_USER.value: "wait",
            },
        )
        builder.add_edge("tools", END)
        builder.add_edge("observe", END)
        return builder.compile(checkpointer=InMemorySaver())

    def display_events(self) -> list[Any]:
        """取出带 ``display_data`` 的状态事件（即待决内容的投影）。"""

        return [event for event in self.projected if event.display_data is not None]

    def projected_request_ids(self) -> list[str]:
        """按投影顺序取被投影的请求标识。"""

        return [
            event.display_data["user_input_request"]["request_id"]
            for event in self.display_events()
        ]

    async def start(self, observations: list[dict[str, Any]], *, lifecycle: Any = _UNSET) -> None:
        """首次进入：注入 state 并驱动到挂起或结束。"""

        await self.graph.ainvoke(_state(observations, lifecycle=lifecycle), self.config)

    async def resume(self, payload: dict[str, Any]) -> None:
        """模拟续跑入口：置位重放标记后注入恢复值。"""

        self.runtime_config.resuming_wait_user = True
        await self.graph.ainvoke(Command(resume=payload), self.config)

    async def snapshot(self) -> Any:
        """取当前断点快照。"""

        return await self.graph.aget_state(self.config)


def test_wait_user_node_passes_through_without_declared_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """观察里没有待决声明时直通 observe：不挂起、不投影、不迁移 Run 状态。"""

    harness = _Harness(monkeypatch)

    async def _run() -> None:
        await harness.start([_observation(request=None)])
        assert (await harness.snapshot()).next == ()

    asyncio.run(_run())
    assert harness.calls == ["observe"]
    assert harness.migrations == []
    assert harness.projected == []


def test_wait_user_node_projects_declared_request_before_suspending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """挂起前必须把待决内容投影到前端：否则用户看到一张空白卡片、也无从作答。"""

    harness = _Harness(monkeypatch)

    async def _run() -> None:
        await harness.start([_observation()])
        suspended = await harness.snapshot()
        assert suspended.next == ("wait",)
        assert any(task.interrupts for task in suspended.tasks)
        # 待决请求不是 state 字段：它是观察上的派生结果，因此不存在可与观察分叉的第二事实源。
        assert "user_input_hold" not in ReactGraphState.model_fields

    asyncio.run(_run())
    (event,) = harness.display_events()
    assert event.tool_call_id == "call-a"
    assert event.status == "running"
    # 投影负责把领域事实组装成前端载荷（唯一组装点）。
    assert event.display_data["user_input_request"] == {
        "request_id": "12",
        "request_kind": "agent_team_review",
        "prompt": "确认执行方案",
        "decisions": ["approve", "reject"],
        "draft_schema": "agent-team-review-v1",
        "draft": {"goal": "交付报告"},
    }
    assert harness.migrations == ["mark"]
    # 挂起前还要把同一份载荷持久化成占位结果行：part 的动态数据只从该行读取，进程重启后冷重建
    # 靠它把卡片与表单恢复出来（见 test_waiting_request_row_rebuilds_approval_card）。
    rows = harness.waiting_rows()
    assert set(rows) == {"call-a"}
    assert rows["call-a"]["run_id"] == 99
    assert rows["call-a"]["transport_metadata"]["status"] == "running"
    assert rows["call-a"]["transport_metadata"]["display_data"]["user_input_request"] == {
        "request_id": "12",
        "request_kind": "agent_team_review",
        "prompt": "确认执行方案",
        "decisions": ["approve", "reject"],
        "draft_schema": "agent-team-review-v1",
        "draft": {"goal": "交付报告"},
    }


def test_wait_user_node_consumes_decision_without_reprojecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恢复会从节点开头重放：重放那一次不得二次投影、也不得重复迁移 Run 状态或重复写占位行。"""

    harness = _Harness(monkeypatch)

    async def _run() -> None:
        await harness.start([_observation()])
        await harness.resume(
            {"decisions": [{"request_id": "12", "decision": "approve", "data": {}}]}
        )
        assert (await harness.snapshot()).next == ()

    asyncio.run(_run())
    assert len(harness.display_events()) == 1
    assert harness.migrations == ["mark"]
    assert harness.runtime_config.resuming_wait_user is False
    # 占位行只写一次：重放若再写一遍，会把它覆盖成同内容（幂等），但那是无意义的数据库往返。
    assert set(harness.waiting_rows()) == {"call-a"}
    assert len(harness.context_writes) == 1


def test_wait_user_node_approval_reopens_gate_and_clears_declaration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批准 = 重开执行门控（记录回到未起跑 + 附决定）+ 清掉声明，随后回 tools 真正执行。"""

    harness = _Harness(monkeypatch)

    async def _run() -> None:
        await harness.start([_observation()])
        await harness.resume(
            {
                "decisions": [
                    {"request_id": "12", "decision": "approve", "data": {"goal": "交付报告"}},
                ]
            }
        )
        resumed = await harness.snapshot()
        assert resumed.next == ()
        record = resumed.values["tool_call_lifecycle"].valid_calls["call-a"]
        # 记录的终态由 observe 在重执行后结算，本节点只负责「重置为未起跑 + 附上决定」。
        assert record.status == "pending"
        assert record.user_decision is not None
        assert record.user_decision.kind is UserDecisionKind.APPROVE
        assert record.user_decision.data == {"goal": "交付报告"}
        # 声明被清掉：否则下一次派生会把它重新当成待决。
        observation = resumed.values["last_tool_results"]["observations"][0]
        assert observation["user_input_request"] is None
        # 批准的观察不被改写为取消：真实结果由重执行产出并覆盖。
        assert observation["status"] == "success"

    asyncio.run(_run())
    assert harness.calls == ["tools"]
    assert harness.migrations == ["mark"]


def test_wait_user_node_rejection_inlines_feedback_and_routes_to_observe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """驳回不进 tools：用户意见内联进观察（取消终态、清掉声明），交给 observe 收口。"""

    harness = _Harness(monkeypatch)

    async def _run() -> None:
        await harness.start([_observation()])
        await harness.resume(
            {
                "decisions": [
                    {"request_id": "12", "decision": "reject", "data": {"feedback": "目标太宽泛"}},
                ]
            }
        )
        snapshot = await harness.snapshot()
        assert snapshot.next == ()
        observation = snapshot.values["last_tool_results"]["observations"][0]
        assert observation["status"] == "cancelled"
        assert "目标太宽泛" in observation["reason"]
        assert observation["display_data"]["status"] == "rejected"
        assert observation["user_input_request"] is None
        # 驳回不重开执行门控：记录仍停在第一遍执行后的 running，等 observe 结算。
        assert snapshot.values["tool_call_lifecycle"].valid_calls["call-a"].status == "running"

    asyncio.run(_run())
    assert harness.calls == ["observe"]


def test_wait_user_node_partial_decisions_materialize_then_self_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只答了一部分时：已作答的立刻物化，剩余请求自环重新挂起，下一轮只投影仍未答的。"""

    harness = _Harness(monkeypatch)
    observations = [
        _observation(),
        _observation(tool_call_id="call-b", request_id="13"),
    ]

    async def _run() -> None:
        await harness.start(observations)
        await harness.resume(
            {"decisions": [{"request_id": "12", "decision": "approve", "data": {}}]}
        )
        snapshot = await harness.snapshot()
        assert snapshot.next == ("wait",)
        # 已批准的那条已经物化（记录回到未起跑），不会因为自环而丢掉用户决定。
        record = snapshot.values["tool_call_lifecycle"].valid_calls["call-a"]
        assert record.status == "pending"
        assert record.user_decision is not None
        # 未答的那条仍带着声明，等下一轮继续问。
        undeclared = [
            observation["user_input_request"]
            for observation in snapshot.values["last_tool_results"]["observations"]
        ]
        assert undeclared[0] is None
        assert undeclared[1] is not None

    asyncio.run(_run())
    assert harness.calls == []
    # 首次挂起迁移一次；自环重新挂起前再迁移一次（中间那次消费恢复值不迁移）。
    assert harness.migrations == ["mark", "mark"]
    # 首次投影两条；自环那轮只投影仍未答的 call-b。
    assert harness.projected_request_ids() == ["12", "13", "13"]
    # 占位行同样只补仍未决的那条；已批准那条的真实结果会覆盖它的占位行，因此一条调用始终一行。
    assert set(harness.waiting_rows()) == {"call-a", "call-b"}
    assert len(harness.context_writes) == 2


def test_wait_user_node_second_resume_executes_earlier_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批准分散在两轮给出时，前一轮的批准不得被漏在未起跑状态：必须回 tools 执行。"""

    harness = _Harness(monkeypatch)
    observations = [
        _observation(),
        _observation(tool_call_id="call-b", request_id="13"),
    ]

    async def _run() -> None:
        await harness.start(observations)
        await harness.resume(
            build_resume_payload([UserDecision(request_id="12", kind=UserDecisionKind.APPROVE)])
        )
        assert (await harness.snapshot()).next == ("wait",)

        await harness.resume(
            build_resume_payload([UserDecision(request_id="13", kind=UserDecisionKind.REJECT)])
        )
        assert (await harness.snapshot()).next == ()

    asyncio.run(_run())
    # 本轮只收到驳回，但第一轮批准的 call-a 仍停在「未起跑 + 带决定」⇒ 必须回 tools。
    assert harness.calls == ["tools"]


def test_wait_user_node_rejects_decisions_for_unknown_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """决定指向本批未声明的请求时必须显式失败，不能静默忽略。"""

    harness = _Harness(monkeypatch)

    async def _run() -> None:
        await harness.start([_observation()])
        with pytest.raises(ValueError, match="未知的等待请求"):
            await harness.resume(
                {"decisions": [{"request_id": "999", "decision": "approve", "data": {}}]}
            )

    asyncio.run(_run())


def test_wait_user_node_rejects_undeclared_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工具未声明的决定必须被拒绝（例如只声明批准的工具收到放弃）。"""

    harness = _Harness(monkeypatch)
    observations = [_observation(decisions=("approve",))]

    async def _run() -> None:
        await harness.start(observations)
        with pytest.raises(ValueError, match="不接受决定"):
            await harness.resume(
                {"decisions": [{"request_id": "12", "decision": "abort", "data": {}}]}
            )

    asyncio.run(_run())


def test_wait_user_node_approval_of_blocked_call_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批准不能扩大权限：本轮被工具白名单拦下的调用不可凭用户决定执行。"""

    harness = _Harness(monkeypatch)
    observations = [_observation("hidden-1")]

    async def _run() -> None:
        await harness.start(observations, lifecycle=_lifecycle("call-a", blocked=("hidden-1",)))
        with pytest.raises(ValueError, match="白名单"):
            await harness.resume(
                {"decisions": [{"request_id": "12", "decision": "approve", "data": {}}]}
            )

    asyncio.run(_run())
    assert harness.calls == []


def test_wait_user_node_requires_lifecycle_when_requests_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有待决请求却没有生命周期快照即 state 已分叉：必须显式失败，不能静默放行。"""

    harness = _Harness(monkeypatch)

    async def _run() -> None:
        with pytest.raises(RuntimeError, match="tool_call_lifecycle is required"):
            await harness.start([_observation()], lifecycle=None)

    asyncio.run(_run())


def test_wait_user_node_empty_resume_value_suspends_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恢复值没有带回决定时自环重新挂起，而不是把空值当作放行。"""

    harness = _Harness(monkeypatch)

    async def _run() -> None:
        await harness.start([_observation()])
        await harness.resume({"decisions": []})
        snapshot = await harness.snapshot()
        assert snapshot.next == ("wait",)
        assert any(task.interrupts for task in snapshot.tasks)

    asyncio.run(_run())
    assert harness.calls == []
    assert harness.migrations == ["mark", "mark"]
    # 自环是一次新的挂起（``resuming_wait_user`` 已复位），因此会再投影一次；投影是
    # ``running`` 自迁移，重复下发同值展示数据是幂等的。
    assert harness.projected_request_ids() == ["12", "12"]
