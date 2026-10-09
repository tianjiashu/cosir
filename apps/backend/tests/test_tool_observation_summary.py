"""工具观察摘要契约测试：``tools`` 节点产出与 ``observe`` 节点消费必须共用一套键名。

回归背景：``tools`` 节点产出的摘要键名一度是旧别名 ``call_id``（由已被删除的摘要治理
模块产出），与执行层字段名 ``tool_call_id`` 不一致，导致任何工具调用都在 observe 节点
抛 ``KeyError: 'call_id'``，run 直接失败。现在摘要直接是本批 ``ToolObservation`` 的
``dataclasses.asdict`` 投影，本模块固定两条契约：

- 摘要键名与执行层观察字段一致（``tool_call_id``），不得再出现 graph state 内部别名；
- ``tools`` 节点产出的摘要必须能被 ``observe`` 节点直接结算（成功与失败观察均覆盖）。
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

import app.core.workflows.react.node_helper.tool_call_lifecycle as lifecycle_module
from app.core.runtime.run_result import ToolRunResult
from app.core.tools.schemas import ToolObservation
from app.core.workflows.react.node_helper.tool_call_lifecycle import (
    ToolCallLifecycleManager,
    ToolCallLifecycleRecord,
)
from app.core.workflows.react.nodes import observation_node as observe_module
from app.core.workflows.react.nodes import tools_node as tools_module
from app.core.workflows.react.worflow_state.route import ReactRoute
from app.core.workflows.react.worflow_state.state import ReactGraphState


def _state(**overrides: Any) -> ReactGraphState:
    """构造最小可执行的 ReAct graph state。

    ``tool_request`` 是历史 state 字段，现在只作为用例声明「本批有哪些调用」的输入：真正的调用
    集合由 ``tool_call_lifecycle`` 承载（``model`` 节点写入），因此这里把它翻译成一份未起跑的快照。
    """

    values: dict[str, Any] = {
        "step_count": 1,
        "tool_error_count": 0,
        "next_node": ReactRoute.MODEL,
        "instruction": "",
        "max_steps": 10,
        "final_text": "",
        "last_tool_results": {"instruction": "", "observations": []},
        "tool_call_lifecycle": _pending_lifecycle(overrides.get("tool_request")),
    }
    values.update(overrides)
    return ReactGraphState(**values)


def _pending_lifecycle(tool_request: dict[str, Any] | None) -> ToolCallLifecycleManager:
    """把用例声明的调用集合构造成本批「尚未起跑」的生命周期快照。"""

    calls = (tool_request or {}).get("tool_calls", [])
    return ToolCallLifecycleManager(
        allows_tools=("list_directory",),
        valid_calls={
            call["id"]: ToolCallLifecycleRecord(
                tool_call_id=call["id"],
                tool_name=call["name"],
                status="pending",
            )
            for call in calls
        },
    )


class _WorkflowHarness:
    """为 tools/observe 节点提供最小运行时依赖，并收集事件与模型消息。"""

    def __init__(self, observations: list[ToolObservation]) -> None:
        self.events: list[Any] = []
        self.messages: list[Any] = []

        async def run_tool_calls(*_args: Any, **_kwargs: Any) -> ToolRunResult:
            """模拟工具批次执行；当前契约下 ``tools`` 节点对 ``run_tool_calls`` 使用 await。"""
            return ToolRunResult(observations=observations)

        self.operations = SimpleNamespace(
            model_tools=[SimpleNamespace(name="list_directory", display=None)],
            all_vaild_tools=[SimpleNamespace(name="list_directory", display=None)],
            allows_tools=frozenset({"list_directory"}),
            get_current_task=lambda: SimpleNamespace(id=1),
            get_current_run=lambda: SimpleNamespace(id=2),
            is_current_run_cancelled=lambda: False,
            run_tool_calls=run_tool_calls,
            to_tool_model_message=lambda observation: ToolMessage(
                content=observation.content, tool_call_id=observation.tool_call_id
            ),
        )
        # ``run`` 供 ``begin`` 发 running 事件时定位（run_id）；缺失会在工具节点内 AttributeError。
        self.runtime_config = SimpleNamespace(
            operations=self.operations,
            run=SimpleNamespace(id=2),
            usage_stats=None,
        )

        def add_message(message: Any, **_kwargs: Any) -> str:
            self.messages.append(message)
            return "appended"

        self.runtime_context = SimpleNamespace(
            add_message=add_message, load_message=lambda: []
        )

    def patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """把 tools/observe/lifecycle 三个模块的运行期依赖绑定到本桩。"""

        monkeypatch.setattr(tools_module, "_runtime_config", lambda: self.runtime_config)
        monkeypatch.setattr(observe_module, "_runtime_config", lambda: self.runtime_config)
        monkeypatch.setattr(lifecycle_module, "_runtime_config", lambda: self.runtime_config)
        monkeypatch.setattr(lifecycle_module, "_runtime_context", lambda: self.runtime_context)
        monkeypatch.setattr(lifecycle_module, "get_stream_writer", lambda: self.events.append)


def test_tools_summary_uses_tool_call_id_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """tools 节点产出的摘要必须使用执行层键名 ``tool_call_id``。"""

    harness = _WorkflowHarness(
        [
            ToolObservation(
                tool_name="list_directory",
                status="success",
                content="dir app",
                tool_call_id="call-1",
                display_data={"kind": "directory-list"},
            )
        ]
    )
    harness.patch(monkeypatch)

    tools_result = asyncio.run(
        tools_module._tools_node(
            _state(
                tool_request={
                    "tool_calls": [{"name": "list_directory", "args": {}, "id": "call-1"}]
                },
                instruction="看看目录",
            )
        )
    )

    summaries = tools_result["last_tool_results"]
    assert summaries["instruction"] == "看看目录"
    assert summaries["observations"][0]["tool_call_id"] == "call-1"
    assert "call_id" not in summaries["observations"][0]


def test_tools_summary_feeds_observe_without_key_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tools 节点产出的摘要必须能被 observe 节点直接结算，不得抛 KeyError。"""

    harness = _WorkflowHarness(
        [
            ToolObservation(
                tool_name="list_directory",
                status="success",
                content="dir app",
                tool_call_id="call-1",
                display_data={"kind": "directory-list"},
            )
        ]
    )
    harness.patch(monkeypatch)
    state = _state(
        tool_request={"tool_calls": [{"name": "list_directory", "args": {}, "id": "call-1"}]},
        instruction="看看目录",
    )

    tools_result = asyncio.run(tools_module._tools_node(state))
    summaries = tools_result["last_tool_results"]

    observe_result = asyncio.run(
        observe_module._observe_node(
            _state(
                next_node=ReactRoute.TOOLS,
                last_tool_results=summaries,
                tool_call_lifecycle=tools_result["tool_call_lifecycle"],
            )
        )
    )

    assert observe_result["tool_error_count"] == 0
    status_events = [event for event in harness.events if event.type == "tool_call_status_changed"]
    assert [event.status for event in status_events] == ["running", "completed"]
    assert [event.tool_call_id for event in status_events] == ["call-1", "call-1"]
    assert isinstance(harness.messages[-1], ToolMessage)
    assert harness.messages[-1].tool_call_id == "call-1"


def test_observe_settles_failed_observation_from_tools_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工具执行失败的摘要同样按 tool_call_id 结算，并累加连续失败计数。"""

    harness = _WorkflowHarness(
        [
            ToolObservation(
                tool_name="list_directory",
                status="error",
                content="cannot list directory",
                tool_call_id="call-9",
                error="list failed",
                reason="path missing",
                retryable=False,
                display_data={"kind": "directory-list", "status_hint": "目录不存在"},
            )
        ]
    )
    harness.patch(monkeypatch)

    tools_result = asyncio.run(
        tools_module._tools_node(
            _state(
                tool_request={
                    "tool_calls": [{"name": "list_directory", "args": {}, "id": "call-9"}]
                },
                instruction="",
            )
        )
    )
    summaries = tools_result["last_tool_results"]
    assert summaries["observations"][0]["tool_call_id"] == "call-9"

    observe_result = asyncio.run(
        observe_module._observe_node(
            _state(
                next_node=ReactRoute.TOOLS,
                last_tool_results=summaries,
                tool_call_lifecycle=tools_result["tool_call_lifecycle"],
            )
        )
    )

    assert observe_result["tool_error_count"] == 1
    status_events = [event for event in harness.events if event.type == "tool_call_status_changed"]
    assert [event.status for event in status_events] == ["running", "failed"]
    assert status_events[-1].error == "目录不存在"
    assert isinstance(harness.messages[-1], ToolMessage)
    assert harness.messages[-1].tool_call_id == "call-9"
