"""``tools`` 节点的执行契约：只执行「尚未起跑」的调用并登记本批结果事实。

覆盖：
- 执行集合 = ``pending`` 记录（首次执行由 model 建好、批准后重执行由 ``wait_user`` 重置），
  两种来源同构，因此本节点不需要知道调用为何待执行；
- ``begin`` 把送执行层的全部记录（含禁用 / 未注册这类隐藏调用）迁出 ``pending``，重入不二次执行；
- 本批观察恒定按 ``tool_call_id`` 合并进本模型步批次；
- 本节点完全不参与用户审批：不登记待决请求、不投影、不消费决定、patch 里没有待决字段。
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from app.assistant_transport.event import ToolCallStatusChangedEvent
from app.core.runtime.run_result import ToolRunResult
from app.core.tools.schemas import ToolObservation, UserDecision, UserDecisionKind
from app.core.workflows.react.node_helper.tool_call_lifecycle import (
    ToolCallLifecycleManager,
    ToolCallLifecycleRecord,
)
from app.core.workflows.react.nodes.tools_node import _merge_batch_results
from app.core.workflows.react.worflow_state.route import ReactRoute
from app.core.workflows.react.worflow_state.state import ReactGraphState

lifecycle_module = importlib.import_module(
    "app.core.workflows.react.node_helper.tool_call_lifecycle"
)
tools_node_module = importlib.import_module("app.core.workflows.react.nodes.tools_node")

_TOOL_NAME = "agent_team"


class _FakeStreamWriter:
    """收集 lifecycle 发出的事件。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def __call__(self, event: Any) -> None:
        self.events.append(event)

    @property
    def status_events(self) -> list[ToolCallStatusChangedEvent]:
        return [event for event in self.events if isinstance(event, ToolCallStatusChangedEvent)]


class _Harness:
    """``_tools_node`` 所需的最小运行时装配（假 stream writer / operations / 工具执行）。"""

    def __init__(self, calls: list[dict[str, Any]], *, observations: list[ToolObservation]) -> None:
        self.writer = _FakeStreamWriter()
        self.calls = calls
        self.observations = observations
        self.messages: list[Any] = []
        self.model_tools = [SimpleNamespace(name=_TOOL_NAME, display=None)]
        self.operations = SimpleNamespace(
            all_vaild_tools=self.model_tools,
            model_tools=self.model_tools,
            allows_tools=frozenset({_TOOL_NAME}),
            get_current_task=lambda: SimpleNamespace(id=11),
            run_tool_calls=self._run_tool_calls,
            to_tool_model_message=lambda observation: ToolMessage(
                content=str(observation.content or observation.error or ""),
                tool_call_id=observation.tool_call_id,
                name=observation.tool_name,
            ),
        )
        self.runtime_config = SimpleNamespace(
            operations=self.operations,
            run=SimpleNamespace(id=200),
            usage_stats=SimpleNamespace(),
        )
        self.runtime_context = SimpleNamespace(
            add_message=lambda message, **kwargs: self.messages.append(message) or "appended"
        )

    async def _run_tool_calls(
        self, task_id: int, calls: list[Any], step_id: str, loop: Any
    ) -> ToolRunResult:
        """记录本批实际执行的调用（含用户决定），并返回预置观察。"""

        self.calls.extend(
            {"call_id": call.call_id, "decision": call.user_decision} for call in calls
        )
        return ToolRunResult(observations=self.observations)

    @contextmanager
    def runtime(self) -> Iterator[None]:
        """注入 lifecycle 与 tools 节点使用的运行时依赖。"""

        import unittest.mock as mock

        with mock.patch.multiple(
            lifecycle_module,
            _runtime_config=lambda: self.runtime_config,
            _runtime_context=lambda: self.runtime_context,
            get_stream_writer=lambda: self.writer,
        ), mock.patch.object(
            tools_node_module, "_runtime_config", lambda: self.runtime_config
        ):
            yield


def _record(
    call_id: str,
    *,
    status: str = "running",
    decision: UserDecision | None = None,
    part_projected: bool = True,
) -> ToolCallLifecycleRecord:
    """构造一条生命周期记录（默认已按第一遍执行迁移过）。"""

    return ToolCallLifecycleRecord(
        tool_call_id=call_id,
        tool_name=_TOOL_NAME,
        status=status,
        args={"team_id": call_id},
        part_projected=part_projected,
        user_decision=decision,
    )


def _observation(call_id: str, **overrides: Any) -> ToolObservation:
    """构造工具执行观察。"""

    payload: dict[str, Any] = {
        "tool_name": _TOOL_NAME,
        "status": "success",
        "content": "started",
        "error": "",
        "reason": "",
        "retryable": False,
        "tool_call_id": call_id,
        "display_data": {},
    }
    payload.update(overrides)
    return ToolObservation(**payload)


def _state(
    lifecycle: ToolCallLifecycleManager | None,
    *,
    last_tool_results: dict[str, Any] | None = None,
) -> ReactGraphState:
    """构造 ``_tools_node`` 所需的最小 state。"""

    return ReactGraphState(
        step_count=3,
        tool_error_count=0,
        next_node=ReactRoute.TOOLS,
        instruction="调用 Team",
        max_steps=10,
        final_text="",
        last_tool_results=last_tool_results or {},
        terminal_sessions={},
        tool_call_lifecycle=lifecycle,
    )


def test_only_pending_calls_are_executed_and_all_of_them_leave_pending() -> None:
    """执行集合只取 ``pending``；已起跑的不再发起，隐藏调用同样算已起跑。"""

    calls: list[dict[str, Any]] = []
    harness = _Harness(
        calls,
        observations=[_observation("call-new"), _observation("hidden-1")],
    )
    lifecycle = ToolCallLifecycleManager(
        allows_tools=(_TOOL_NAME,),
        valid_calls={
            "call-done": _record("call-done", status="running"),
            "call-new": _record("call-new", status="pending"),
        },
        blocked_calls={"hidden-1": _record("hidden-1", status="pending", part_projected=False)},
    )

    with harness.runtime():
        patch = asyncio.run(tools_node_module._tools_node(_state(lifecycle)))

    assert [item["call_id"] for item in calls] == ["call-new", "hidden-1"]
    assert all(item["decision"] is None for item in calls)
    after = patch["tool_call_lifecycle"]
    assert after.valid_calls["call-new"].status == "running"
    # 隐藏调用（前端无 part）同样被迁出 ``pending``：否则重入会把它再送一次执行层。
    assert after.blocked_calls["hidden-1"].status == "running"
    assert [item["tool_call_id"] for item in patch["last_tool_results"]["observations"]] == [
        "call-new",
        "hidden-1",
    ]


def test_reentry_after_begin_executes_nothing() -> None:
    """同一批记录已全部迁出 ``pending`` 时重入不执行任何调用（避免有副作用工具二次写入）。"""

    calls: list[dict[str, Any]] = []
    harness = _Harness(calls, observations=[])
    lifecycle = ToolCallLifecycleManager(
        allows_tools=(_TOOL_NAME,),
        valid_calls={"call-a": _record("call-a", status="running")},
        blocked_calls={"hidden-1": _record("hidden-1", status="running", part_projected=False)},
    )

    with harness.runtime():
        patch = asyncio.run(tools_node_module._tools_node(_state(lifecycle)))

    assert calls == []
    assert patch["last_tool_results"]["observations"] == []


def test_approved_replay_runs_the_reopened_call_with_its_decision() -> None:
    """批准后重执行：本节点只按 ``pending`` 判据执行被 ``wait_user`` 重置的那条调用。

    重执行门控（``reopen_for_approved_replay``）与决定落库都在 ``wait_user``：本节点既不识别
    「批准」这一概念，也不读取任何待决请求列表，只执行「尚未起跑」的记录。
    """

    calls: list[dict[str, Any]] = []
    harness = _Harness(calls, observations=[_observation("call-team", content="running")])
    decision = UserDecision(request_id="12", kind=UserDecisionKind.APPROVE)
    lifecycle = ToolCallLifecycleManager(
        allows_tools=(_TOOL_NAME,),
        valid_calls={
            # 被 wait_user 重置回未起跑，并附上用户决定。
            "call-team": _record("call-team", status="pending", decision=decision),
            # 同批其它调用第一遍已起跑，仍在 running，不得被重跑。
            "call-other": _record("call-other", status="running"),
        },
    )
    previous = {
        "instruction": "调用 Team",
        "observations": [
            {"tool_call_id": "call-team", "tool_name": _TOOL_NAME, "status": "success"},
            {"tool_call_id": "call-other", "tool_name": _TOOL_NAME, "status": "success"},
        ],
    }

    with harness.runtime():
        patch = asyncio.run(
            tools_node_module._tools_node(_state(lifecycle, last_tool_results=previous))
        )

    assert [item["call_id"] for item in calls] == ["call-team"]
    assert calls[0]["decision"] is decision
    # 结果按调用合并：同批其它调用的旧观察必须保留（否则模型侧配对出现空洞）。
    merged = patch["last_tool_results"]["observations"]
    assert [item["tool_call_id"] for item in merged] == ["call-team", "call-other"]
    assert merged[0]["content"] == "running"


def test_tools_node_never_projects_or_registers_pending_requests() -> None:
    """带待决声明的结果也只是普通观察：不投影给前端，patch 里没有待决字段。"""

    calls: list[dict[str, Any]] = []
    display_data = {"kind": "agent-team-preview", "status": "pending"}
    harness = _Harness(
        calls,
        observations=[
            _observation(
                "call-team",
                display_data=display_data,
                user_input_request={
                    "kind": "agent_team_review",
                    "request_id": "17",
                    "decisions": ["approve", "reject"],
                },
            )
        ],
    )
    lifecycle = ToolCallLifecycleManager(
        allows_tools=(_TOOL_NAME,),
        valid_calls={"call-team": _record("call-team", status="pending")},
    )

    with harness.runtime():
        patch = asyncio.run(tools_node_module._tools_node(_state(lifecycle)))

    # 本节点只登记这三样事实：待决请求随观察原样进入本批摘要，没有额外的「待决列表」通道。
    assert set(patch) == {"last_tool_results", "terminal_sessions", "tool_call_lifecycle"}
    # 本节点不发带 display_data 的状态事件：投影是 wait_user 在挂起前的职责。
    assert [event for event in harness.writer.status_events if event.display_data is not None] == []
    # 请求随观察原样进入本批摘要（由 wait_user 派生），本节点不解读它。
    (summary,) = patch["last_tool_results"]["observations"]
    assert summary["user_input_request"]["request_id"] == "17"


def test_missing_lifecycle_fails_loudly() -> None:
    """缺少工具调用生命周期时显式失败，而不是把空批次当正常路径静默执行。"""

    calls: list[dict[str, Any]] = []
    harness = _Harness(calls, observations=[])

    with harness.runtime(), pytest.raises(RuntimeError, match="tool_call_lifecycle is required"):
        asyncio.run(tools_node_module._tools_node(_state(None)))

    assert calls == []


# --- 批次合并 ---------------------------------------------------------------------------


def test_merge_batch_results_overwrites_same_call_and_appends_new_calls() -> None:
    """按 tool_call_id 覆盖同一条调用的结果，并保留同批其它调用（否则模型侧配对出现空洞）。"""

    previous = {
        "instruction": "调用 Team",
        "observations": [
            _observation_dict("call-a"),
            _observation_dict("call-b"),
        ],
    }

    merged = _merge_batch_results(
        previous,
        [_observation_dict("call-a", content="已启动"), _observation_dict("call-c")],
        instruction="调用 Team",
    )

    assert merged["instruction"] == "调用 Team"
    assert [item["tool_call_id"] for item in merged["observations"]] == [
        "call-a",
        "call-b",
        "call-c",
    ]
    assert merged["observations"][0]["content"] == "已启动"
    assert merged["observations"][1]["tool_call_id"] == "call-b"


def test_merge_batch_results_tolerates_missing_previous_observations() -> None:
    """首次执行（state 里没有 observations 键）时按新批次处理。"""

    merged = _merge_batch_results({}, [_observation_dict("call-a")], instruction="")

    assert merged["instruction"] == ""
    assert len(merged["observations"]) == 1


def test_merge_batch_results_instruction_comes_from_argument_not_previous() -> None:
    """instruction 由调用方显式给出，不从既有批次继承。

    批次起点为空（``observe`` 结算后清空 ``last_tool_results``）时 ``previous`` 里没有
    instruction，若从它读取就会把模型本步的伴随文本丢掉。
    """

    merged = _merge_batch_results(
        {"instruction": "上一批的说明", "observations": []},
        [_observation_dict("call-a")],
        instruction="本批的说明",
    )

    assert merged["instruction"] == "本批的说明"


def test_merge_batch_results_collapses_observations_without_call_id() -> None:
    """缺失 ``tool_call_id`` 的观察按空键合并（同键后者覆盖），不产生重复行。"""

    merged = _merge_batch_results(
        {},
        [
            {"tool_call_id": "", "content": "第一次"},
            {"tool_call_id": "", "content": "第二次"},
        ],
        instruction="",
    )

    assert [item["content"] for item in merged["observations"]] == ["第二次"]


def _observation_dict(call_id: str, *, content: str = "观察") -> dict[str, Any]:
    """构造一条观察摘要（形状与 ``dataclasses.asdict`` 投影一致）。"""

    return {
        "tool_name": _TOOL_NAME,
        "status": "success",
        "content": content,
        "error": "",
        "reason": "",
        "retryable": False,
        "tool_call_id": call_id,
        "display_data": {},
        "user_input_request": None,
    }
