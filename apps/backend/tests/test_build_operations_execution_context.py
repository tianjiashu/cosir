"""``AgentRuntime._build_operations`` 注入 execution_context 链路验证。

聚焦验证根因修复：``_build_operations`` 必须把 ``_resolve_execution_context``
的结果作为 ``execution_context`` 传入 ``RuntimeOperations``，否则门面内部
``self._execution_context`` 恒为 None，工具执行会抛 ``execution_context is required``。

为避免拉起完整 ``AgentRuntime`` / 真实存储依赖图，这里：
- 用 ``object.__new__`` 绕过 ``AgentRuntime.__init__``（其会强制初始化 tool_system）；
- 用 stub ``_workspace_service`` 让 ``_resolve_execution_context`` 命中并返回非 None；
- 用 ``mock.patch`` 替换 ``RuntimeOperations`` 类，仅捕获构造实参断言注入行为。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest import mock

from app.core.runtime import runner as runner_module
from app.core.runtime.runner import AgentRuntime
from app.tools.schemas.tool_execution_context import ToolExecutionContext


def _make_workspace(workspace_id: str) -> SimpleNamespace:
    """构造最小 workspace 记录（仅需 workspace_id）。"""

    return SimpleNamespace(workspace_id=workspace_id, name="ws", root_path="H:/x")


def _make_task(workspace_id: str, task_id: str) -> SimpleNamespace:
    """构造最小 task 记录（仅需 workspace_id / task_id）。"""

    return SimpleNamespace(workspace_id=workspace_id, task_id=task_id)


def _make_turn(turn_id: str) -> SimpleNamespace:
    """构造最小 turn 记录（仅需 turn_id）。"""

    return SimpleNamespace(turn_id=turn_id)


def test_build_operations_injects_resolved_execution_context() -> None:
    """_build_operations 必须把解析出的 execution_context 注入 RuntimeOperations。"""

    # 绕过 __init__，仅 stub 本测试所需内部依赖。
    runtime = object.__new__(AgentRuntime)
    runtime._tool_scheduler = SimpleNamespace(list_tools=lambda: [])
    runtime._agent_registry = SimpleNamespace()
    runtime._turn_service = SimpleNamespace()

    # stub workspace_service：让 _resolve_execution_context 命中并返回非 None。
    def _get_workspace(workspace_id: str):
        return _make_workspace(workspace_id)

    runtime._workspace_service = SimpleNamespace(get_workspace=_get_workspace)

    workspace = _make_workspace("ws-1")
    task = _make_task("ws-1", "task-1")
    turn = _make_turn("turn-1")
    agent_profile = SimpleNamespace(select_tools=lambda tools: tools)

    captured: dict = {}
    captured_lock = threading.Lock()

    def _fake_runtime_operations(**kwargs):
        with captured_lock:
            captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    with (
        mock.patch.object(runner_module, "RuntimeOperations", side_effect=_fake_runtime_operations),
        mock.patch.object(runner_module, "get_delegation_service", return_value=SimpleNamespace()),
    ):
        runtime._build_operations(
            workspace=workspace,
            task=task,
            turn=turn,
            agent_profile=agent_profile,
            tool_trace_recorder=None,
        )

    assert "execution_context" in captured, "RuntimeOperations 未收到 execution_context"
    assert captured["execution_context"] is not None, (
        "execution_context 为 None：runner 未把 _resolve_execution_context 结果注入门面，"
        "将复现 'execution_context is required'"
    )
    assert isinstance(
        captured["execution_context"], ToolExecutionContext
    ), "注入的 execution_context 类型不符，下游工具执行会因契约不匹配而失败"
    assert (
        captured["execution_context"].turn_id == "turn-1"
    ), "execution_context 未携带当前 turn_id，取消/回退关联会错位"
