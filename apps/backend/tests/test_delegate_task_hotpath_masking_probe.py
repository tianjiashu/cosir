"""独立对抗探针：注册表持续故障时，delegate_task 契约是否仍「失败安全」。

背景（与当前实现对齐）：
- ``delegate_task`` 的工具定义**不再**按 workspace 投影候选——既无 ``enum`` 也不列举 ID，
  子 Agent 目录由系统提示词的工具能力目录层下发（见 ``child_agent_create.to_definition``
  的 docstring）。因此「定义投影降级」这一旧风险面已不存在，本文件只保留仍成立的部分。
- 目标合法性由 ``DelegateTaskTool.execute`` 在执行期按 workspace 作用域解析 Registry 裁决，
  ``ToolAccessGate`` 只做「注册表命中 + 权限 + 参数 schema + Hook」，不解析 child agent id。

本文件用探针量化剩余风险边界：注册表本身持续抛异常时，定义构建与准入门禁都不应崩，
非法 child_agent_id 也不会在准入层被硬失败信号拦住（授权完全后置到 handler）。
"""

from pathlib import Path

import pytest

from app.config import configuration
from app.core.tools.schemas.tool_call import ToolCall
from app.core.tools.schemas.tool_execution_context import ToolExecutionContext
from app.core.tools.tool_execute.tool_access_gate import ToolAccessGate
from app.core.tools.tool_handler.child_task import child_agent_create
from app.core.tools.tool_models.child_task.delegate_task_args import DelegateTaskArgs
from app.core.tools.tool_registry import ToolRegistry


class _PermanentlyBrokenRegistry:
    """持续抛异常的注册表替身：模拟注册表实现缺陷（非一次性时序问题）。"""

    def child_agent_ids(self) -> set[str]:
        raise AttributeError("persistent registry bug")

    def child_agent_summary(self) -> str:
        raise AttributeError("persistent registry bug")


@pytest.fixture
def broken_registry(monkeypatch) -> _PermanentlyBrokenRegistry:
    """注入一个持续故障的进程级注册表。"""

    registry = _PermanentlyBrokenRegistry()
    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", registry, raising=False)
    return registry


@pytest.fixture
def delegate_registry(broken_registry: _PermanentlyBrokenRegistry) -> ToolRegistry:
    """在注册表持续故障的进程状态下，只装配 delegate_task 的工具注册表。

    直接调用工具类的 ``to_definition``（不实例化 handler，故不解析 Task/Run 服务单例），
    使本探针只聚焦「定义构建 + 准入门禁」这一段，不牵连完整 ``ToolSystem`` 的存储依赖。
    """

    definition = child_agent_create.DelegateTaskTool.to_definition(
        object.__new__(child_agent_create.DelegateTaskTool)
    )
    registry = ToolRegistry()
    registry.register(definition)
    return registry


def test_broken_registry_does_not_break_definition_projection(
    delegate_registry: ToolRegistry,
) -> None:
    """[探针] 注册表持续故障时，delegate_task 定义仍可投影给模型且不固化候选。

    目的：固化「定义构建不读取 agent 注册表」这一契约——注册表坏掉不应让定义构建失败，
    也不应在 schema 里出现候选 ``enum``（候选目录已移到系统提示词层）。
    潜在缺陷：定义构建重新耦合注册表，注册表故障即让工具无法下发。
    """

    definition = delegate_registry.get_tool_definition("delegate_task")
    assert definition is not None
    params = definition.to_model_tool_definition()["parameters"]
    assert "enum" not in params["properties"]["child_agent_id"]


def test_broken_registry_disables_hard_validation(monkeypatch) -> None:
    """[探针] 注册表持续故障 → 参数模型放行任意非法 id（授权后置到 handler）。"""

    monkeypatch.setattr(
        configuration, "_AGENT_REGISTRY", _PermanentlyBrokenRegistry(), raising=False
    )

    args = DelegateTaskArgs(child_agent_id="totally-fake", agent_name="t", message="p")
    assert args.child_agent_id == "totally-fake"


def test_broken_registry_admits_invalid_id_through_gate(
    delegate_registry: ToolRegistry,
) -> None:
    """[关键探针] 注册表持续故障时，非法 id 仍能通过准入门禁（不提供硬失败）。

    这是「授权边界」的量化证据：门禁只校验结构，不解析 child agent id，因此非法 id
    会一路进入业务层，最终由 ``DelegateTaskTool.execute`` 按 workspace Registry 裁决
    （该裁决已有独立用例覆盖，见 ``test_delegate_task_model_contract``）。
    """

    gate = ToolAccessGate(delegate_registry)
    ctx = ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=Path.cwd())
    call = ToolCall(
        tool_name="delegate_task",
        arguments={"child_agent_id": "fake-id", "agent_name": "t", "message": "p"},
        call_id="c1",
    )

    outcome = gate.evaluate(call, ctx)

    # 记录实际行为：门禁放行（不崩），且参数原样透传给业务层。这不是缺陷本身，
    # 而是「授权后置」的边界事实。
    assert outcome.admitted is True, "若此处为 False，说明门禁比预期更保守（更好）"
    assert outcome.arguments["child_agent_id"] == "fake-id"
