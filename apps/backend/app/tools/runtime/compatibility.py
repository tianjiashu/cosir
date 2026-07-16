"""旧工具调度接口的 Tool v2 兼容门面。"""

import logging
from typing import Iterable, List

from app.tools.execution.records import ToolPolicyDecision
from app.tools.execution.approval import ToolApprovalPolicy
from app.tools.executor import ToolCallExecutor
from app.tools.registry.memory import ToolRegistry
from app.tools.results import ToolObservationBuilder
from app.tools.runtime.platform import ToolRuntime
from app.tools.types import ToolCall, ToolDefinition, ToolObservation


class ToolScheduler:
    """保留旧调用接口，并将执行委托给 Tool v2 Runtime。"""

    def __init__(
        self,
        registry: ToolRegistry,
        allowed_permissions: Iterable[str],
        logger: logging.Logger,
        approval_required_permissions: Iterable[str] = (),
    ) -> None:
        """初始化兼容调度门面。

        参数:
            registry: 已注册工具的唯一 handler 查找入口。
            allowed_permissions: 可直接执行的权限集合。
            logger: 用于工具诊断的日志器。
            approval_required_permissions: 需要人工审批的权限集合。

        返回:
            无。

        异常:
            无。

        副作用:
            创建仅服务旧调用方的 Tool v2 Runtime 适配实例。
        """

        self._registry = registry
        self._logger = logger
        self._approval_policy = ToolApprovalPolicy(
            auto_approved_permissions=allowed_permissions,
            approval_required_permissions=approval_required_permissions,
        )
        # 旧接口保留完整输出语义；生产 ToolRuntime 使用默认摘要限制与 artifact 化。
        observation_builder = ToolObservationBuilder(max_content_chars=16 * 1024 * 1024)
        self._runtime = ToolRuntime(
            registry=registry,
            executor=ToolCallExecutor(observation_builder, logger),
            observation_builder=observation_builder,
            logger=logger,
            compatibility_policy=self._compatibility_decision,
        )

    def list_model_visible_tools(self) -> List[ToolDefinition]:
        """返回旧调度器策略允许模型看见的工具。

        参数:
            无。

        返回:
            模型可见的已注册工具定义。

        异常:
            无。

        副作用:
            无。
        """

        return [tool for tool in self._runtime.list_model_visible_tools() if self._approval_policy.is_visible_to_model(tool)]

    def execute(self, call: ToolCall) -> ToolObservation:
        """通过 Tool v2 Runtime 执行一次旧接口工具调用。

        参数:
            call: 模型请求的工具调用。

        返回:
            归一化工具观测结果。

        异常:
            无。平台可预期错误被转换为观测结果。

        副作用:
            可能执行 handler 副作用并写入兼容模式日志。
        """

        self._logger.info(
            "tool_scheduler_execute_started",
            extra={"tool_name": call.tool_name, "tool_call_id": call.call_id},
        )
        return self._runtime.execute_single_tool_call(call)

    def _compatibility_decision(self, tool: ToolDefinition) -> ToolPolicyDecision:
        """将旧审批策略映射为 Tool v2 策略决策。

        参数:
            tool: 正在评估的已注册工具。

        返回:
            Tool v2 统一策略决策。

        异常:
            无。

        副作用:
            无。
        """

        decision = self._approval_policy.decide(tool)
        if decision.status == "approved":
            return ToolPolicyDecision("allow", tool.risk_level, decision.reason, tool.permission)
        if decision.status == "approval_required":
            return ToolPolicyDecision("approval_required", tool.risk_level, decision.reason, tool.permission)
        return ToolPolicyDecision("deny", tool.risk_level, decision.reason, tool.permission)
