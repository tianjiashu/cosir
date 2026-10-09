"""ToolCallLifecycleManager 的状态、事件和序列化契约测试。"""

from types import SimpleNamespace
from typing import Any

from langchain_core.messages import ToolMessage

import app.core.workflows.react.node_helper.tool_call_lifecycle as lifecycle_module
from app.core.tools.schemas import ToolCall
from app.core.workflows.react.node_helper.tool_call_lifecycle import (
    SettlementResult,
    ToolCallLifecycleManager,
    ToolCallLifecycleRecord,
)
from app.core.workflows.react.worflow_state.route import ReactRoute
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
        # 补全 WorkflowOperations 契约：``_valid_tool_name`` / ``_presentation_for``
        # 经 ``operations.all_vaild_tools`` 取工具名与展示声明，harness 必须提供该属性，
        # 否则走 ``create`` / ``classify`` 的用例会因 mock 不完整而 AttributeError。
        model_tools = model_tools or [SimpleNamespace(name="read_file", display=None)]
        self.operations = SimpleNamespace(
            to_tool_model_message=lambda observation: ("tool-message", observation.tool_call_id),
            model_tools=model_tools,
            all_vaild_tools=model_tools,
        )
        self.runtime_config = SimpleNamespace(operations=self.operations)

        def add_message(message: Any, **_kwargs: Any) -> str:
            self.messages.append(message)
            return "appended"

        self.runtime_context = SimpleNamespace(add_message=add_message)
        self.manager = ToolCallLifecycleManager(
            allows_tools=tuple(tool.name for tool in self.operations.model_tools)
        )

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
            self.manager.valid_calls.setdefault(
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
        """创建工具调用并保存返回的 state 快照（参数在创建期落进记录）。"""

        with self._patch_runtime():
            self.manager = self.manager.create(
                task_id=1,
                run_id=2,
                step_id="step-3",
                raw_tool_calls=[
                    {
                        "id": tool_call.call_id,
                        "name": tool_call.tool_name,
                        "args": tool_call.arguments,
                    }
                ],
            )

    def begin(self, call_id: str) -> None:
        """把已创建的工具调用迁移为 running（迁移只按状态判定，不再重传参数）。"""

        with self._patch_runtime():
            self.manager = self.manager.begin(
                task_id=1,
                run_id=2,
                step_id="step-3",
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
    assert harness.manager.valid_calls["a"].status == "completed"


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
    harness.create(
        ToolCall(tool_name="read_file", call_id="call-1", arguments={"path": "a.py"})
    )
    assert harness.manager is not original
    assert harness.manager.valid_calls["call-1"].status == "pending"
    assert harness.events[0].type == "tool_call_created"

    harness.begin("call-1")
    assert harness.manager.valid_calls["call-1"].status == "running"
    assert harness.manager.valid_calls["call-1"].args == {"path": "a.py"}
    assert harness.events[1].args == {"path": "a.py"}

    harness.cancel()
    assert harness.manager.valid_calls["call-1"].status == "cancelled"
    assert harness.events[2].status == "cancelled"

    dumped = harness.manager.model_dump(mode="json")
    assert dumped["valid_calls"]["call-1"]["status"] == "cancelled"
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
    assert set(harness.manager.valid_calls) == {"a", "b"}


def test_banned_tool_is_filtered_from_live_projection_and_execution() -> None:
    """禁用调用只进入隐藏闭合集合，不发事件也不进入可执行集合。"""

    harness = _LifecycleHarness()
    harness.manager = ToolCallLifecycleManager(allows_tools=("write_file",))
    harness.create(ToolCall(tool_name="read_file", call_id="blocked-call"))

    with harness._patch_runtime():
        harness.manager = harness.manager.classify(
            tool_calls=[{"name": "read_file", "args": {}, "id": "blocked-call"}],
            invalid_tool_calls=[],
        )

    assert harness.events == []
    assert harness.manager.valid_calls == {}
    assert harness.manager.valid_tools == []
    # 隐藏闭合的调用同样算「已登记」：``has_call`` 覆盖两个集合，只是前端没有对应 part。
    assert harness.manager.has_call
    assert [record.tool_call_id for record in harness.manager.blocked_calls.values()] == [
        "blocked-call"
    ]


def test_mixed_banned_tool_only_projects_and_executes_allowed_call() -> None:
    """混合批次只抑制禁用工具，启用工具保留原生命周期行为。"""

    harness = _LifecycleHarness(
        [
            SimpleNamespace(name="read_file", display=None),
            SimpleNamespace(name="write_file", display=None),
        ]
    )
    harness.manager = ToolCallLifecycleManager(allows_tools=("write_file",))
    with harness._patch_runtime():
        harness.manager = harness.manager.create(
            task_id=1,
            run_id=2,
            step_id="step-3",
            raw_tool_calls=[
                {"id": "blocked", "name": "read_file"},
                {"id": "allowed", "name": "write_file"},
            ],
        )
        harness.manager = harness.manager.classify(
            tool_calls=[
                {"name": "read_file", "args": {}, "id": "blocked"},
                {"name": "write_file", "args": {}, "id": "allowed"},
            ],
            invalid_tool_calls=[],
        )

    # ``classify`` 只重建集合、不发事件；创建事件由 ``create`` 发出（此处只有一条 allowed）。
    assert [event.tool_call_id for event in harness.events] == ["allowed"]
    assert [record.tool_call_id for record in harness.manager.valid_tools] == ["allowed"]
    assert [record.tool_call_id for record in harness.manager.blocked_calls.values()] == ["blocked"]


def test_blocked_call_closure_is_settled_without_status_event() -> None:
    """禁用调用的模型协议闭合由 ``settle`` 负责：写 ToolMessage，但不发终态事件。

    禁用调用同样会送执行层（由门禁产出拒绝观察），因此它走正常结算路径；它没有前端 part，
    事件必须被跳过（判据与 ``cancel`` 共用 ``part_projected``）。
    """

    # 注册工具集含 read_file（真实禁用场景：已注册但不在本轮白名单），
    # 仅 write_file 在本轮 allows_tools 中。
    harness = _LifecycleHarness(
        [
            SimpleNamespace(name="read_file", display=None),
            SimpleNamespace(name="write_file", display=None),
        ]
    )
    harness.manager = ToolCallLifecycleManager(allows_tools=("write_file",))
    with harness._patch_runtime():
        harness.manager = harness.manager.create(
            task_id=1,
            run_id=2,
            step_id="step-3",
            raw_tool_calls=[{"id": "blocked", "name": "read_file"}],
        )
        harness.manager, event_status = harness.manager.settle(
            task_id=1,
            run_id=2,
            step_id="step-3",
            summary=_summary(
                tool_call_id="blocked",
                status="error",
                error="tool is not allowed for this run",
            ),
        )

    assert event_status == "failed"
    assert len(harness.messages) == 1
    assert harness.events == [], "隐藏闭合不得发终态事件"
    assert harness.manager.blocked_calls["blocked"].status == "failed"


def test_graph_state_checkpoint_restores_lifecycle_manager() -> None:
    """state checkpoint 解码后仍恢复为 manager，而不是普通 dict。"""

    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    state = ReactGraphState(
        step_count=0,
        tool_error_count=0,
        next_node=ReactRoute.MODEL,
        instruction="",
        max_steps=1,
        final_text="",
        last_tool_results={},
        tool_call_lifecycle=ToolCallLifecycleManager(
            allows_tools=("read_file",),
            valid_calls={
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
    assert restored.tool_call_lifecycle.valid_calls["call-1"].status == "running"
    assert restored.tool_call_lifecycle.allows_tools == ("read_file",)
