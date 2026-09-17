"""独立对抗探针：投影热路径「失败安全」是否掩盖真实缺陷。

修复引入两类「静默降级」：
1. ``ToolDefinition.project_*`` 对 provider 异常降级；
2. ``DelegateTaskArgs`` 在候选集不可用时**不拦截**非法 child_agent_id。

二者叠加的潜在风险：一旦 agent 注册表本身存在持续性缺陷（``child_agent_ids()``
长期抛异常），enforcement（enum + 硬校验）会被**同时**绕过，原始缺陷形态
「非法 child_agent_id 一路流到业务层」即静默复现，且无任何硬失败信号（只有 WARNING）。

本文件用探针量化该风险，用于给出「可接受边界」的结论依据，而非修改生产代码。
"""

import logging

import pytest

from app.config import configuration
from app.core.tools.schemas.tool_call import ToolCall
from app.core.tools.tool_execute.tool_access_gate import ToolAccessGate
from app.core.tools.tool_models.delegate_task_args import DelegateTaskArgs
from app.core.tools.tool_system import ToolSystem


class _PermanentlyBrokenRegistry:
    """持续抛异常的注册表替身：模拟注册表实现缺陷（非一次性时序问题）。"""

    def child_agent_ids(self) -> set[str]:
        raise AttributeError("persistent registry bug")

    def child_agent_summary(self) -> str:
        raise AttributeError("persistent registry bug")


@pytest.fixture
def tool_system_with_broken_registry(monkeypatch) -> ToolSystem:
    """构建工具系统后注入一个持续故障的注册表。"""

    monkeypatch.setattr(configuration, "_TOOL_SYSTEM", None, raising=False)
    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", _PermanentlyBrokenRegistry(), raising=False)
    tool_system = ToolSystem.build_tool_system()
    configuration.set_tool_system(tool_system)
    return tool_system


def test_broken_registry_drops_enum(tool_system_with_broken_registry: ToolSystem) -> None:
    """[探针] 注册表持续故障 → 模型可见 schema 无 enum（enforcement 第一层失效）。"""

    definition = tool_system_with_broken_registry.registry.get_tool_definition("delegate_task")
    assert definition is not None
    params = definition.to_model_tool_definition()["parameters"]
    assert "enum" not in params["properties"]["child_agent_id"]


def test_broken_registry_disables_hard_validation(monkeypatch) -> None:
    """[探针] 注册表持续故障 → 硬校验放行任意非法 id（enforcement 第二层失效）。"""

    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", _PermanentlyBrokenRegistry(), raising=False)

    args = DelegateTaskArgs(child_agent_id="totally-fake", title="t", prompt="p")
    assert args.child_agent_id == "totally-fake"


def test_broken_registry_admits_invalid_id_through_gate(
    tool_system_with_broken_registry: ToolSystem,
) -> None:
    """[关键探针] 两层 enforcement 同时失效时，非法 id 能通过准入门禁。

    这是「可接受边界」判定的量化证据：注册表持续故障下，修复后的系统**不提供任何
    硬失败**，非法 id 会一路进入业务层。仅记录 WARNING。
    """

    from pathlib import Path

    from app.core.tools.schemas.tool_execution_context import ToolExecutionContext

    gate = ToolAccessGate(tool_system_with_broken_registry.registry)
    ctx = ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=Path.cwd())
    call = ToolCall(tool_name="delegate_task", arguments={"child_agent_id": "fake-id", "title": "t", "prompt": "p"}, call_id="c1")

    outcome = gate.evaluate(call, ctx)

    # 记录实际行为：门禁放行（不崩）。这不是缺陷本身，而是「静默降级」的边界事实。
    assert outcome.admitted is True, "若此处为 False，说明降级路径比预期更保守（更好）"


def test_broken_registry_still_logs_warning_events(
    tool_system_with_broken_registry: ToolSystem, caplog
) -> None:
    """[探针] 至少必须留下可观测的 WARNING，否则静默降级无从排查。"""

    definition = tool_system_with_broken_registry.registry.get_tool_definition("delegate_task")
    assert definition is not None

    with caplog.at_level(logging.WARNING):
        definition.to_model_tool_definition()

    events = {r.getMessage() for r in caplog.records}
    assert "delegate_task_child_agent_catalog_unavailable" in events
    assert "delegate_agent_catalog_unavailable" in events
