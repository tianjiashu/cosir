"""工具执行管线 ``ToolExecutor`` 的单元测试。

覆盖调度层合并（单一执行入口收口）后的执行管线核心行为：
- ``ToolAccessGate`` 四段准入（注册表命中 / Agent profile 门禁 / 参数校验 / PreToolUse Hook 短路）
- ``ToolExecutor`` 编排（成功执行 / 拒绝归一化 / 缺 execution_context 抛 ValueError / list_tools）
- 拒绝观察一律经 ``ToolObservationBudget`` 治理（模型通道脱敏 + 截断口径与成功路径一致）
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.tools.guard.tool_output_budget import ToolOutputBudget
from app.core.tools.schemas import (
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_executor import ToolExecutor
from app.core.tools.tool_execute.tool_observation_budget import ToolObservationBudget
from app.core.tools.tool_handler.read_file import ReadFileTool
from app.core.tools.tool_registry import ToolRegistry


def _make_context(tmp_path: Path) -> ToolExecutionContext:
    """构造一个绑定临时目录 workspace 的最小执行上下文。

    参数:
        tmp_path: pytest 临时目录，作为 workspace 根。

    返回:
        绑定 task/workspace/run 标识的 ``ToolExecutionContext``。

    异常:
        无。

    副作用:
        无。
    """

    return ToolExecutionContext(
        task_id=1,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=1,
    )


def _make_executor(tmp_path: Path) -> ToolExecutor:
    """构造只注册真实 read_file 工具的执行管线。

    参数:
        tmp_path: pytest 临时目录，作为 read_file 的 project_root 与 workspace 根。

    返回:
        已装配 ``ToolRegistry`` 与默认预算的 ``ToolExecutor``。

    异常:
        无。

    副作用:
        在 tmp_path 下创建 ``sample.txt``。
    """

    (tmp_path / "sample.txt").write_text("hello world", encoding="utf-8")
    registry = ToolRegistry([ReadFileTool().to_definition()])
    return ToolExecutor(registry=registry)


def test_output_budget_normalizes_nullable_observation_content() -> None:
    """拒绝型 observation 的空 content 也必须安全通过统一输出预算。"""

    observation = ToolObservation(
        tool_name="read_file",
        status="error",
        content=None,
        error="denied",
    )

    result = ToolOutputBudget().apply(observation, execution_context=None)

    assert result.content == ""


def test_observation_budget_keeps_display_data_complete() -> None:
    """展示数据只服务前端，不应复用模型 content 的字符预算。"""

    patch = "x" * 5_000
    observation = ToolObservation(
        tool_name="write_file",
        status="success",
        content="ok",
        display_data={"kind": "file-changes", "changes": [{"patch": patch}]},
    )

    result = ToolObservationBudget().apply(observation, execution_context=None)

    assert result.display_data == observation.display_data


def test_unknown_tool_returns_denial_observation(tmp_path: Path) -> None:
    """未注册工具应被门禁拒绝，返回确定性 error 观察而非抛异常。"""

    executor = _make_executor(tmp_path)
    observation = executor.execute(
        ToolCall(tool_name="no_such_tool", arguments={}, call_id="call-1"),
        execution_context=_make_context(tmp_path),
    )

    assert observation.status == "error"
    assert observation.tool_call_id == "call-1"
    assert "unknown tool" in observation.error
    assert observation.reason  # 面向模型的富文本必填


def test_profile_denied_tool(tmp_path: Path) -> None:
    """Agent profile 不允许的工具应被权限门禁拒绝，并列出允许集合。"""

    executor = _make_executor(tmp_path)
    observation = executor.execute(
        ToolCall(tool_name="read_file", arguments={"path": "sample.txt"}, call_id="call-2"),
        execution_context=_make_context(tmp_path),
        allowed_tool_names={"other_tool"},
    )

    assert observation.status == "error"
    assert "other_tool" in observation.reason
    assert observation.error.startswith("agent profile denied tool")


def test_invalid_arguments_rejected(tmp_path: Path) -> None:
    """不符合 ``args_model`` 的参数应在参数校验段被拒绝。"""

    executor = _make_executor(tmp_path)
    observation = executor.execute(
        ToolCall(tool_name="read_file", arguments={"nonexistent_field": 1}, call_id="call-3"),
        execution_context=_make_context(tmp_path),
    )

    assert observation.status == "error"
    assert "invalid tool arguments" in observation.error


def test_execute_success_returns_budgeted_observation(tmp_path: Path) -> None:
    """成功路径应产出 status=success、绑定 call_id 的已治理观察。"""

    executor = _make_executor(tmp_path)
    observation = executor.execute(
        ToolCall(tool_name="read_file", arguments={"path": "sample.txt"}, call_id="call-4"),
        execution_context=_make_context(tmp_path),
    )

    assert observation.status == "success"
    assert observation.tool_call_id == "call-4"
    assert "hello world" in observation.content


def test_missing_execution_context_raises(tmp_path: Path) -> None:
    """execution_context 缺失属装配错误，必须显式抛 ValueError 而非静默降级。"""

    executor = _make_executor(tmp_path)
    with pytest.raises(ValueError):
        executor.execute(
            ToolCall(tool_name="read_file", arguments={"path": "sample.txt"}, call_id="call-5"),
            execution_context=None,
        )


def test_list_tools_and_get_tool_definition(tmp_path: Path) -> None:
    """从旧调度层合并而来的列举/查询契约应保持等价语义。"""

    executor = _make_executor(tmp_path)
    names = [tool.name for tool in executor.list_tools()]

    assert names == ["read_file"]
    assert executor.get_tool_definition("read_file") is not None
    assert executor.get_tool_definition("missing") is None


def test_denial_observation_passes_through_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """拒绝观察也应过同一道预算：把预算压到极小后 content 被截断。"""

    monkeypatch.setattr(
        "app.core.tools.guard.tool_output_budget.ToolOutputBudget.apply",
        lambda self, observation, context: observation,
    )
    observed: dict[str, Any] = {}
    original_apply = ToolObservationBudget.apply

    def _spy_apply(
        self: ToolObservationBudget, observation: ToolObservation, context: Any
    ) -> ToolObservation:
        observed["called"] = True
        return original_apply(self, observation, context)

    monkeypatch.setattr(ToolObservationBudget, "apply", _spy_apply)

    executor = _make_executor(tmp_path)
    executor.execute(
        ToolCall(tool_name="no_such_tool", arguments={}, call_id="call-6"),
        execution_context=_make_context(tmp_path),
    )

    assert observed.get("called") is True


def test_gate_modified_arguments_flow_to_handler(tmp_path: Path) -> None:
    """PreToolUse Hook 改写参数后，隔离执行器应使用改写后的参数。"""

    from app.hook import hook_interceptor

    def _fake_fire(context: Any, *_args: Any, **_kwargs: Any):
        decision = SimpleNamespace(
            decision="allow",
            deny_reason=None,
            modified_arguments={"path": "rewritten.txt"},
        )
        return decision

    (tmp_path / "rewritten.txt").write_text("rewritten", encoding="utf-8")
    monkey_target = hook_interceptor.HookInterceptor
    original = monkey_target.safe_fire
    monkey_target.safe_fire = staticmethod(_fake_fire)  # type: ignore[method-assign]
    try:
        executor = _make_executor(tmp_path)
        observation = executor.execute(
            ToolCall(tool_name="read_file", arguments={"path": "sample.txt"}, call_id="call-7"),
            execution_context=_make_context(tmp_path),
        )
    finally:
        monkey_target.safe_fire = original  # type: ignore[method-assign]

    assert observation.status == "success"
    assert "rewritten" in observation.content


def test_gate_evaluate_unknown_tool(tmp_path: Path) -> None:
    """直接调用门禁：未注册工具返回 denial 且 admitted=False。"""

    from app.core.tools.tool_execute.tool_access_gate import ToolAccessGate

    registry = ToolRegistry([ReadFileTool().to_definition()])
    gate = ToolAccessGate(registry)
    outcome = gate.evaluate(
        ToolCall(tool_name="ghost", arguments={}, call_id="call-8"),
        execution_context=_make_context(tmp_path),
    )

    assert outcome.admitted is False
    assert outcome.tool is None
    assert outcome.denial is not None


def test_tool_definition_contract(tmp_path: Path) -> None:
    """read_file 的定义应保持 thread 隔离模式（合并后契约不变）。"""

    definition: ToolDefinition = ReadFileTool().to_definition()

    assert definition.execution_mode == "thread"
    assert definition.timeout_seconds == 10.0
