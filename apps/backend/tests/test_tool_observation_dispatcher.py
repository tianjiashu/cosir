"""工具观察分发器的单元测试。

覆盖 ``tool_observation_dispatcher`` 的核心契约：
- 状态映射：success→completed（携 result）、error→failed（携 error）、cancelled→cancelled
- 计数语义：success 清零、error 累加、cancelled 不计
- 每条摘要同时写回模型上下文（ToolMessage 配对闭合）
"""

from types import SimpleNamespace
from typing import Any

from app.core.workflows.nodes.helper import tool_observation_dispatcher as dispatcher
from app.core.workflows.nodes.helper import tool_observation_summary as summary_module


def _summary(**overrides: object) -> dict[str, Any]:
    """构造一条最小观察摘要 dict。

    参数:
        overrides: 覆盖默认字段的键值。

    返回:
        摘要 dict。

    异常:
        无。

    副作用:
        无。
    """

    base: dict[str, Any] = {
        "call_id": "call-1",
        "tool_name": "read_file",
        "status": "success",
        "error": "",
        "reason": "",
        "content": "file body",
        "retryable": False,
        "data": {},
    }
    base.update(overrides)
    return base


class _DispatcherHarness:
    """分发器测试桩：收集事件与写回消息，替换 LangGraph 与运行时配置上下文。

    参数:
        无。

    异常:
        无。

    副作用:
        patch 模块级依赖（get_stream_writer / _runtime_config / _runtime_context）。
    """

    def __init__(self) -> None:
        """初始化桩并安装 patch。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            记录原函数并替换 ``dispatcher.get_stream_writer`` /
            ``dispatcher._runtime_config`` / ``dispatcher._runtime_context``。
        """

        self.events: list[Any] = []
        self.messages: list[Any] = []
        self._originals: list[tuple[Any, Any]] = []

        def _collect_event(event: Any) -> None:
            self.events.append(event)

        operations = SimpleNamespace(
            to_tool_model_message=lambda summary_obj: ("tool-message", summary_obj.tool_call_id)
        )
        runtime_context = SimpleNamespace(add_message=self.messages.append)
        runtime_config = SimpleNamespace(operations=operations)

        replacements = {
            "get_stream_writer": lambda: _collect_event,
            "_runtime_config": lambda: runtime_config,
            "_runtime_context": lambda: runtime_context,
        }
        for name, replacement in replacements.items():
            self._originals.append((name, getattr(dispatcher, name)))
            setattr(dispatcher, name, replacement)

    def teardown(self) -> None:
        """还原全部 patch。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            恢复被替换的模块属性。
        """

        for name, original in self._originals:
            setattr(dispatcher, name, original)


def test_status_mapping_success_error_cancelled() -> None:
    """三种原始状态应分别映射为 completed/failed/cancelled 且携带对应字段。"""

    harness = _DispatcherHarness()
    try:
        result = dispatcher.dispatch_tool_observations(
            [
                _summary(call_id="a", status="success", content="ok"),
                _summary(call_id="b", status="error", error="boom"),
                _summary(call_id="c", status="cancelled"),
            ],
            task_id=1,
            run_id=2,
            step_id="step-3",
            inherited_error_count=0,
        )

        assert [(e.tool_call_id, e.status) for e in harness.events] == [
            ("a", "completed"),
            ("b", "failed"),
            ("c", "cancelled"),
        ]
        assert harness.events[0].result == "ok"
        assert harness.events[0].data == {}
        assert harness.events[0].error is None
        assert harness.events[1].error == "boom"
        assert harness.events[1].result is None
        assert result == {"tool_error_count": 1, "error_count": 1}
    finally:
        harness.teardown()


def test_data_is_forwarded_to_event_only() -> None:
    """摘要中的 UI data 应进入终态事件，但不进入模型观察消息。"""

    harness = _DispatcherHarness()
    try:
        data = {"kind": "file-list", "files": [{"path": "a.py"}]}
        dispatcher.dispatch_tool_observations(
            [_summary(data=data)],
            task_id=1,
            run_id=2,
            step_id="step-3",
            inherited_error_count=0,
        )

        assert harness.events[0].data == data
        assert harness.messages == [("tool-message", "call-1")]
    finally:
        harness.teardown()


def test_error_count_resets_on_success_and_increments_on_error() -> None:
    """success 清零连续失败计数；error 累加；cancelled 不计。"""

    harness = _DispatcherHarness()
    try:
        result = dispatcher.dispatch_tool_observations(
            [
                _summary(call_id="a", status="error", error="x"),
                _summary(call_id="b", status="error", error="y"),
                _summary(call_id="c", status="cancelled"),
            ],
            task_id=1,
            run_id=2,
            step_id="step-3",
            inherited_error_count=1,
        )
        assert result["tool_error_count"] == 3

        result2 = dispatcher.dispatch_tool_observations(
            [_summary(call_id="d", status="success")],
            task_id=1,
            run_id=2,
            step_id="step-4",
            inherited_error_count=result["tool_error_count"],
        )
        assert result2["tool_error_count"] == 0
    finally:
        harness.teardown()


def test_context_messages_written_in_order() -> None:
    """每条摘要都应写回一条模型上下文消息，顺序与批次一致。"""

    harness = _DispatcherHarness()
    try:
        dispatcher.dispatch_tool_observations(
            [
                _summary(call_id="a", status="success"),
                _summary(call_id="b", status="error", error="e"),
            ],
            task_id=1,
            run_id=2,
            step_id="step-3",
            inherited_error_count=0,
        )

        assert [message[1] for message in harness.messages] == ["a", "b"]
    finally:
        harness.teardown()


def test_unknown_status_falls_back_to_failed() -> None:
    """未知状态应兜底为 failed，保证前端 tool-call part 必达终态。"""

    harness = _DispatcherHarness()
    try:
        result = dispatcher.dispatch_tool_observations(
            [_summary(call_id="a", status="weird")],
            task_id=1,
            run_id=2,
            step_id="step-3",
            inherited_error_count=0,
        )

        assert harness.events[0].status == "failed"
        # 未知状态按失败语义计数（与 error 同口径）。
        assert result == {"tool_error_count": 1, "error_count": 1}
    finally:
        harness.teardown()


def test_summary_roundtrip_to_observation() -> None:
    """摘要转回模型观察对象时应丢弃 UI data。"""

    original = _summary()
    observation = dispatcher._summary_to_observation(original)

    assert observation.tool_call_id == "call-1"
    assert observation.tool_name == "read_file"
    assert observation.status == "success"
    assert observation.content == "file body"
    assert observation.data is None
    # 摘要与构建函数产出的字段一一对应（圆环完整性）。
    rebuilt = summary_module.build_tool_result_summaries([observation])
    assert rebuilt["observations"][0] == original
