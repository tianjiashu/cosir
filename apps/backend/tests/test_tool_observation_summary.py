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

from app.core.runtime.run_result import ToolRunResult
from app.core.tools.schemas import ToolObservation
from app.core.workflows.nodes import observation_node as observe_module
from app.core.workflows.nodes import tools_node as tools_module
from app.core.workflows.nodes.helper import tool_call_lifecycle as lifecycle_module
from app.core.workflows.nodes.helper.tool_call_lifecycle import (
    ToolCallLifecycleManager,
    ToolCallLifecycleRecord,
)
from app.core.workflows.react.state import ReactGraphState


def _state(**overrides: Any) -> ReactGraphState:
    """构造最小可执行的 ReAct graph state。"""

    values: dict[str, Any] = {
        "step_count": 1,
        "tool_error_count": 0,
        "requested_tool": False,
        "final_response": False,
        "terminal": False,
        "instruction": "",
        "max_steps": 10,
        "final_text": "",
        "last_tool_results": {"instruction": "", "observations": []},
        "tool_call_lifecycle": ToolCallLifecycleManager(),
    }
    values.update(overrides)
    return ReactGraphState(**values)


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
            get_current_task=lambda: SimpleNamespace(id=1),
            get_current_run=lambda: SimpleNamespace(id=2),
            is_current_run_cancelled=lambda: False,
            run_tool_calls=run_tool_calls,
            to_tool_model_message=lambda observation: ToolMessage(
                content=observation.content, tool_call_id=observation.tool_call_id
            ),
        )
        self.runtime_config = SimpleNamespace(operations=self.operations, usage_stats=None)
        def add_message(message: Any, **_kwargs: Any) -> None:
            self.messages.append(message)

        self.runtime_context = SimpleNamespace(add_message=add_message, load_message=lambda: [])

    def patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """把 tools/observe/lifecycle 三个模块的运行期依赖绑定到本桩。"""

        monkeypatch.setattr(tools_module, "_runtime_config", lambda: self.runtime_config)
        monkeypatch.setattr(observe_module, "_runtime_config", lambda: self.runtime_config)
        monkeypatch.setattr(observe_module, "_runtime_context", lambda: self.runtime_context)
        monkeypatch.setattr(lifecycle_module, "_runtime_config", lambda: self.runtime_config)
        monkeypatch.setattr(lifecycle_module, "_runtime_context", lambda: self.runtime_context)
        monkeypatch.setattr(lifecycle_module, "get_stream_writer", lambda: self.events.append)


def _running_call(call_id: str, status: str = "running") -> ToolCallLifecycleRecord:
    """构造 lifecycle 中处于给定状态的合法调用记录（供 tools/observe 节点读取 running 调用）。"""

    return ToolCallLifecycleRecord(
        tool_call_id=call_id, tool_name="list_directory", status=status
    )


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
                tool_call_lifecycle=ToolCallLifecycleManager(
                    calls={"call-1": _running_call("call-1")}
                ),
                instruction="看看目录",
            )
        )
    )

    summaries = tools_result["last_tool_results"]
    assert summaries["instruction"] == "看看目录"
    assert summaries["expected_call_ids"] == ["call-1"]
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
        tool_call_lifecycle=ToolCallLifecycleManager(calls={"call-1": _running_call("call-1")}),
        instruction="看看目录",
    )

    tools_result = asyncio.run(tools_module._tools_node(state))
    summaries = tools_result["last_tool_results"]

    observe_result = asyncio.run(
        observe_module._observe_node(
            _state(
                requested_tool=True,
                last_tool_results=summaries,
                tool_call_lifecycle=state.tool_call_lifecycle,
            )
        )
    )

    assert observe_result["tool_error_count"] == 0
    assert [event.status for event in harness.events] == ["completed"]
    assert [event.tool_call_id for event in harness.events] == ["call-1"]
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
                tool_call_lifecycle=ToolCallLifecycleManager(
                    calls={"call-9": _running_call("call-9")}
                ),
                instruction="",
            )
        )
    )
    summaries = tools_result["last_tool_results"]
    assert summaries["observations"][0]["tool_call_id"] == "call-9"

    observe_result = asyncio.run(
        observe_module._observe_node(
            _state(
                requested_tool=True,
                last_tool_results=summaries,
                tool_call_lifecycle=ToolCallLifecycleManager(),
            )
        )
    )

    assert observe_result["tool_error_count"] == 1
    assert [event.status for event in harness.events] == ["failed"]
    assert harness.events[0].error == "目录不存在"
    assert isinstance(harness.messages[-1], ToolMessage)
    assert harness.messages[-1].tool_call_id == "call-9"
