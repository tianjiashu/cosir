"""ToolCallLifecycleManager 的状态、事件和序列化契约测试。"""

from types import SimpleNamespace
from typing import Any

import app.core.workflows.react.node_helper.tool_call_lifecycle as lifecycle_module
from app.core.tools.schemas import ToolCall
from app.core.workflows.react.node_helper.tool_call_lifecycle import (
    SettlementResult,
    ToolCallLifecycleManager,
    ToolCallLifecycleRecord,
)
from app.core.workflows.react.worflow_state.state import ReactGraphState


def _summary(**overrides: object) -> dict[str, Any]:
    """构造最小观察摘要。"""

    base: dict[str, Any] = {
        "tool_call_id": "call-1",
        "tool_name": "read_file",
        "status": "success",
        "error": "",
        "reason": "",
        "content": "file body",
        "retryable": False,
        "display_data": {},
    }
    base.update(overrides)
    return base


class _LifecycleHarness:
    """提供 lifecycle 所需的运行时依赖，并收集事件和模型消息。"""

    def __init__(self, model_tools: list[Any] | None = None) -> None:
        self.events: list[Any] = []
        self.messages: list[Any] = []
        self.operations = SimpleNamespace(
            to_tool_model_message=lambda observation: ("tool-message", observation.tool_call_id),
            model_tools=model_tools or [SimpleNamespace(name="read_file", display=None)],
        )
        self.runtime_config = SimpleNamespace(operations=self.operations)

        def add_message(message: Any, **_kwargs: Any) -> None:
            self.messages.append(message)

        self.runtime_context = SimpleNamespace(add_message=add_message)
        self.manager = ToolCallLifecycleManager()

    def _patch_runtime(self):
        """返回 lifecycle 模块依赖的可恢复 patch_write 上下文。"""

        from unittest.mock import patch

        return patch.multiple(
            lifecycle_module,
            _runtime_config=lambda: self.runtime_config,
            _runtime_context=lambda: self.runtime_context,
            get_stream_writer=lambda: self.events.append,
        )

    def settle_batch(
        self, summaries: list[dict[str, Any]], *, inherited_error_count: int
    ) -> SettlementResult:
        """结算摘要并保留返回的生命周期快照。"""

        for summary in summaries:
            call_id = summary["tool_call_id"]
            self.manager.calls.setdefault(
                call_id,
                ToolCallLifecycleRecord(
                    tool_call_id=call_id,
                    tool_name=summary["tool_name"],
                ),
            )
        with self._patch_runtime():
            result = self.manager.settle_batch(
                task_id=1,
                run_id=2,
                step_id="step-3",
                summaries=summaries,
                inherited_error_count=inherited_error_count,
            )
        self.manager = result.lifecycle
        return result

    def create(self, tool_call: ToolCall) -> None:
        """创建工具调用并保存返回的 state 快照。"""

        with self._patch_runtime():
            self.manager = self.manager.create(
                task_id=1,
                run_id=2,
                step_id="step-3",
                raw_tool_calls=[{"id": tool_call.call_id, "name": tool_call.tool_name}],
            )

    def begin(self, call_id: str, args: dict[str, object] | None = None) -> None:
        """开始一个已创建的工具调用。"""

        with self._patch_runtime():
            self.manager = self.manager.begin(
                task_id=1,
                run_id=2,
                step_id="step-3",
                tool_calls=[ToolCall(tool_name="read_file", call_id=call_id, arguments=args or {})],
            )

    def cancel(self) -> None:
        """收口全部未终态的工具调用（``cancel`` 按状态收口，不接受点名集合）。"""

        with self._patch_runtime():
            self.manager = self.manager.cancel(
                task_id=1,
                run_id=2,
                step_id="step-3",
            )


def test_status_mapping_success_error_cancelled() -> None:
    """三种原始状态分别映射为 completed/failed/cancelled。"""

    harness = _LifecycleHarness()
    result = harness.settle_batch(
        [
            _summary(tool_call_id="a", status="success", content="ok"),
            _summary(
                tool_call_id="b",
                status="error",
                error="boom",
                display_data={"status_hint": "命令失败"},
            ),
            _summary(tool_call_id="c", status="cancelled"),
        ],
        inherited_error_count=0,
    )

    assert [(event.tool_call_id, event.status) for event in harness.events] == [
        ("a", "completed"),
        ("b", "failed"),
        ("c", "cancelled"),
    ]
    assert harness.events[0].display_data == {}
    assert harness.events[0].error is None
    assert harness.events[1].error == "命令失败"
    assert harness.events[2].error == "已取消"
    assert result.tool_error_count == 1 and result.error_count == 1
    assert harness.manager.calls["a"].status == "completed"


def test_data_is_forwarded_to_event_only() -> None:
    """摘要 UI data 进入终态事件，但不进入模型观察消息。"""

    harness = _LifecycleHarness()
    display_data = {"kind": "file-list", "files": [{"path": "a.py"}]}
    harness.settle_batch([_summary(display_data=display_data)], inherited_error_count=0)

    assert harness.events[0].display_data == display_data
    assert harness.messages == [("tool-message", "call-1")]


def test_error_count_resets_on_success_and_increments_on_error() -> None:
    """success 清零连续失败计数；error 累加；cancelled 不计。"""

    harness = _LifecycleHarness()
    result = harness.settle_batch(
        [
            _summary(tool_call_id="a", status="error", error="x"),
            _summary(tool_call_id="b", status="error", error="y"),
            _summary(tool_call_id="c", status="cancelled"),
        ],
        inherited_error_count=1,
    )
    assert result.tool_error_count == 3
    result2 = harness.settle_batch(
        [_summary(tool_call_id="d", status="success")],
        inherited_error_count=result.tool_error_count,
    )
    assert result2.tool_error_count == 0


def test_unknown_status_falls_back_to_failed() -> None:
    """未知状态兜底为 failed 并计入失败次数。"""

    harness = _LifecycleHarness()
    result = harness.settle_batch(
        [_summary(tool_call_id="a", status="weird")], inherited_error_count=0
    )

    assert harness.events[0].status == "failed"
    assert result.tool_error_count == 1 and result.error_count == 1


def test_create_begin_and_cancel_update_serializable_state() -> None:
    """生命周期迁移返回新 manager，state 只包含可序列化字段。"""

    harness = _LifecycleHarness()
    original = harness.manager
    harness.create(ToolCall(tool_name="read_file", call_id="call-1"))
    assert harness.manager is not original
    assert harness.manager.calls["call-1"].status == "pending"
    assert harness.events[0].type == "tool_call_created"

    harness.begin("call-1", {"path": "a.py"})
    assert harness.manager.calls["call-1"].status == "running"
    assert harness.manager.calls["call-1"].args == {"path": "a.py"}
    assert harness.events[1].args == {"path": "a.py"}

    harness.cancel()
    assert harness.manager.calls["call-1"].status == "cancelled"
    assert harness.events[2].status == "cancelled"

    dumped = harness.manager.model_dump(mode="json")
    assert dumped["calls"]["call-1"]["status"] == "cancelled"
    assert "operations" not in dumped
    assert "stream_writer" not in dumped


def test_create_is_idempotent_and_handles_multiple_calls() -> None:
    """同一批多个调用各自创建，重复身份不会重复发事件。"""

    harness = _LifecycleHarness(
        [
            SimpleNamespace(name="read_file", display=None),
            SimpleNamespace(name="write_file", display=None),
        ]
    )
    with harness._patch_runtime():
        harness.manager = harness.manager.create(
            task_id=1,
            run_id=2,
            step_id="step-3",
            raw_tool_calls=[
                {"id": "a", "name": "read_file"},
                {"id": "b", "name": "write_file"},
                {"id": "a", "name": "read_file"},
            ],
        )

    assert [event.tool_call_id for event in harness.events] == ["a", "b"]
    assert set(harness.manager.calls) == {"a", "b"}


def test_graph_state_checkpoint_restores_lifecycle_manager() -> None:
    """state checkpoint 解码后仍恢复为 manager，而不是普通 dict。"""

    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    state = ReactGraphState(
        step_count=0,
        tool_error_count=0,
        requested_tool=False,
        final_response=False,
        terminal=False,
        instruction="",
        max_steps=1,
        final_text="",
        last_tool_results={},
        tool_call_lifecycle=ToolCallLifecycleManager(
            calls={
                "call-1": ToolCallLifecycleRecord(
                    tool_call_id="call-1",
                    tool_name="read_file",
                    status="running",
                )
            }
        ),
    )
    serializer = JsonPlusSerializer()
    restored = serializer.loads_typed(serializer.dumps_typed(state))

    assert isinstance(restored.tool_call_lifecycle, ToolCallLifecycleManager)
    assert restored.tool_call_lifecycle.calls["call-1"].status == "running"
