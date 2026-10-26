"""工具执行管线 ``ToolExecutor`` 的单元测试。

覆盖调度层合并（单一执行入口收口）后的执行管线核心行为：
- ``ToolAccessGate`` 四段准入（注册表命中 / Agent profile 门禁 / 参数校验 / PreToolUse Hook 短路）
- ``ToolExecutor`` 编排（成功执行 / 拒绝归一化 / list_tools）
- running 事件只在真正交给 handler 前由执行器发出一次；门禁拒绝与 stale 拦截不发
- 拒绝观察一律经 ``ToolObservationBudget`` 治理（模型通道长度限制与截断口径和成功路径一致）
"""

from collections.abc import Collection
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.tool.execute.tool_output_budget import ToolOutputBudget
from app.core.tool.schemas import (
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.execute import tool_executor as tool_executor_module
from app.core.tool.execute.tool_executor import ToolExecutor
from app.core.tool.execute.tool_observation_budget import ToolObservationBudget
from app.tools.tool_handler.read_file import ReadFileTool
from app.tools.tool_handler.write_file import WriteFileTool
from app.core.tool.tool_registry import ToolRegistry


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
        step_id="step-1",
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


def _execute(
    executor: ToolExecutor,
    call: ToolCall,
    execution_context: ToolExecutionContext,
    *,
    allowed_tool_names: Collection[str] | None = None,
) -> ToolObservation:
    """以「全部已注册工具都准入」为默认值调用执行器，用例只覆盖它关心的参数。

    与生产调用方 ``run_tool_batch`` 一致，按调用把 ``tool_call_id`` 特化进执行上下文：
    执行器信任二者一致，成功观察的 ``tool_call_id`` 取自执行上下文。
    """

    if allowed_tool_names is None:
        allowed_tool_names = set(executor._registry.get_all_tool_names())
    return executor.execute(
        call,
        execution_context=replace(execution_context, tool_call_id=call.call_id),
        allowed_tool_names=allowed_tool_names,
    )


def test_output_budget_normalizes_nullable_observation_content() -> None:
    """拒绝型 observation 的空 content 也必须安全通过统一输出预算。"""

    observation = ToolObservation(
        tool_name="read_file",
        status="error",
        content=None,
    )

    result = ToolOutputBudget().apply(observation, execution_context=None)

    assert result.content == ""


def test_output_budget_preserves_content_verbatim() -> None:
    """输出预算只限制长度，不改写工具正文。"""

    observation = ToolObservation(
        tool_name="read_file",
        status="success",
        content="api_key=raw-secret",
    )

    result = ToolOutputBudget().apply(observation, execution_context=None)

    assert result.content == "api_key=raw-secret"


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
    observation = _execute(
        executor,
        ToolCall(tool_name="no_such_tool", arguments='{}', call_id="call-1"),
        _make_context(tmp_path),
    )

    assert observation.status == "error"
    assert observation.tool_call_id == "call-1"
    assert "unknown tool" in observation.content
    assert "\nreason: " in observation.content  # 面向模型的富文本必填


def test_profile_denied_tool(tmp_path: Path) -> None:
    """Agent profile 不允许的工具应被权限门禁拒绝，且不可重试。

    门禁刻意不复述策略来源（哪条 profile / 谁关掉的），也不把可用工具集回灌给模型——模型
    本就持有工具声明，回灌只会放大提示面——故 ``reason`` 只回传「本轮禁用、停止调用」。
    """

    executor = _make_executor(tmp_path)
    observation = _execute(
        executor,
        ToolCall(tool_name="read_file", arguments='{"path": "sample.txt"}', call_id="call-2"),
        _make_context(tmp_path),
        allowed_tool_names={"other_tool"},
    )

    assert observation.status == "error"
    assert observation.content.startswith("error: tool is not allowed: read_file\n")
    assert "hint: do not retry this tool call." in observation.content
    assert "\nreason: " in observation.content  # 面向模型的富文本必填
    assert "other_tool" not in observation.content


def test_invalid_arguments_rejected(tmp_path: Path) -> None:
    """不符合 ``args_model`` 的参数应在参数校验段被拒绝。"""

    executor = _make_executor(tmp_path)
    observation = _execute(
        executor,
        ToolCall(tool_name="read_file", arguments='{"nonexistent_field": 1}', call_id="call-3"),
        _make_context(tmp_path),
    )

    assert observation.status == "error"
    assert "invalid tool arguments" in observation.content


@pytest.mark.parametrize("arguments", ['{"path":', "[1]", "null", '{"n":NaN}', ""])
def test_malformed_arguments_rejected_before_validation(tmp_path: Path, arguments: str) -> None:
    """参数解码只在准入门禁发生：残缺 JSON、非对象与非标准常量都形成可重试的错误观察。"""

    executor = _make_executor(tmp_path)
    observation = _execute(
        executor,
        ToolCall(tool_name="read_file", arguments=arguments, call_id="call-malformed"),
        _make_context(tmp_path),
    )

    assert observation.status == "error"
    assert observation.content.startswith("error: Invalid tool arguments\n")
    assert "hint: this error can be retried" in observation.content
    assert observation.tool_call_id == "call-malformed"


def test_execute_success_returns_budgeted_observation(tmp_path: Path) -> None:
    """成功路径应产出 status=success、绑定 call_id 的已治理观察。"""

    executor = _make_executor(tmp_path)
    observation = _execute(
        executor,
        ToolCall(tool_name="read_file", arguments='{"path": "sample.txt"}', call_id="call-4"),
        _make_context(tmp_path),
    )

    assert observation.status == "success"
    assert observation.tool_call_id == "call-4"
    assert "hello world" in observation.content


def test_file_write_executes_without_change_tracking_service(tmp_path: Path) -> None:
    """File editors write normally when no optional change tracking is configured."""

    target = tmp_path / "created.txt"
    executor = ToolExecutor(registry=ToolRegistry([WriteFileTool().to_definition()]))

    observation = _execute(
        executor,
        ToolCall(
            tool_name="write_file",
            arguments='{"path": "created.txt", "content": "untracked"}',
            call_id="call-write-without-service",
        ),
        _make_context(tmp_path),
    )

    assert observation.status == "success"
    assert target.read_text(encoding="utf-8") == "untracked"


def test_denial_observation_passes_through_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """拒绝观察也应过同一道预算：把预算压到极小后 content 被截断。"""

    monkeypatch.setattr(
        "app.core.tool.execute.tool_output_budget.ToolOutputBudget.apply",
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
    _execute(
        executor,
        ToolCall(tool_name="no_such_tool", arguments='{}', call_id="call-6"),
        _make_context(tmp_path),
    )

    assert observed.get("called") is True


def test_gate_modified_arguments_flow_to_handler(tmp_path: Path) -> None:
    """PreToolUse Hook 改写参数后，隔离执行器应使用改写后的参数。"""

    from app.core.hook import hook_interceptor

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
        observation = _execute(
        executor,
            ToolCall(tool_name="read_file", arguments='{"path": "sample.txt"}', call_id="call-7"),
            _make_context(tmp_path),
        )
    finally:
        monkey_target.safe_fire = original  # type: ignore[method-assign]

    assert observation.status == "success"
    assert "rewritten" in observation.content


def test_gate_evaluate_unknown_tool(tmp_path: Path) -> None:
    """直接调用门禁：未注册工具返回 denial 且 admitted=False。"""

    from app.core.tool.execute.tool_access_gate import ToolAccessGate

    registry = ToolRegistry([ReadFileTool().to_definition()])
    gate = ToolAccessGate(registry)
    outcome = gate.evaluate(
        ToolCall(tool_name="ghost", arguments='{}', call_id="call-8"),
        _make_context(tmp_path),
    )

    assert outcome.admitted is False
    assert outcome.tool is None
    assert outcome.denial is not None


def test_tool_definition_contract(tmp_path: Path) -> None:
    """read_file 的定义应保持 thread 隔离模式（合并后契约不变）。"""

    definition: ToolDefinition = ReadFileTool().to_definition()

    assert definition.execution_mode == "thread"
    assert definition.timeout_seconds == 10.0


@pytest.fixture(autouse=True)
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """截获执行器发出的执行事件；执行器单测不在 workflow 运行作用域内，不走真实 sink。"""

    events: list[Any] = []
    monkeypatch.setattr(tool_executor_module, "emit", events.append)
    return events


def _record_running(order: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tool_executor_module, "emit", lambda event: order.append(f"emit:{event.status}")
    )


def test_running_emitted_once_before_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """成功路径上执行器恰好发出一次 running，且早于 handler 运行。"""

    from app.core.tool.execute.tool_handler_runner import ToolHandlerRunner

    order: list[str] = []
    original_execute = ToolHandlerRunner.execute

    def _spy_execute(self: ToolHandlerRunner, *args: Any, **kwargs: Any) -> ToolObservation:
        order.append("handler")
        return original_execute(self, *args, **kwargs)

    monkeypatch.setattr(ToolHandlerRunner, "execute", _spy_execute)
    _record_running(order, monkeypatch)
    executor = _make_executor(tmp_path)
    observation = _execute(
        executor,
        ToolCall(tool_name="read_file", arguments='{"path": "sample.txt"}', call_id="call-start"),
        _make_context(tmp_path),
    )

    assert observation.status == "success"
    assert order == ["emit:running", "handler"]


def test_running_event_carries_call_identity_and_display_arguments(
    tmp_path: Path, emitted: list[Any]
) -> None:
    """running 由执行器发出：带调用身份、step 与准入后的最终参数，运行期即可渲染参数。"""

    executor = _make_executor(tmp_path)
    _execute(
        executor,
        ToolCall(tool_name="read_file", arguments='{"path": "sample.txt"}', call_id="call-args"),
        _make_context(tmp_path),
    )

    [running] = [event for event in emitted if event.status == "running"]
    assert running.tool_call_id == "call-args"
    assert running.step_id == _make_context(tmp_path).step_id
    # 已过准入：展示的是校验后的最终参数（含 schema 默认值），不是原始参数串。
    assert running.args is not None and running.args["path"] == "sample.txt"
    assert set(running.args) > {"path"}


@pytest.mark.parametrize(
    ("call", "allowed"),
    [
        (ToolCall(tool_name="no_such_tool", arguments="{}", call_id="c"), None),
        (ToolCall(tool_name="read_file", arguments='{"path": "sample.txt"}', call_id="c"), set()),
        (ToolCall(tool_name="read_file", arguments='{"path":', call_id="c"), None),
        (ToolCall(tool_name="read_file", arguments='{"nonexistent_field": 1}', call_id="c"), None),
    ],
    ids=["unknown_tool", "disallowed", "malformed_json", "invalid_schema"],
)
def test_running_not_emitted_when_gate_denies(
    tmp_path: Path, call: ToolCall, allowed: set[str] | None, emitted: list[Any]
) -> None:
    """准入门禁拒绝的调用从未开始运行：不发 running，前端也就不会先显示为运行中。"""

    executor = _make_executor(tmp_path)
    observation = _execute(
        executor,
        call,
        _make_context(tmp_path),
        allowed_tool_names=allowed,
    )

    assert observation.status == "error"
    assert emitted == []
