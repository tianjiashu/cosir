"""Tool Platform 唯一对外执行入口。"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Callable, Optional

from app.domain.approvals.service import ApprovalService
from app.tools.execution.concurrency import ToolConcurrencyPlanner
from app.tools.execution.idempotency import ToolIdempotencyKeyBuilder
from app.tools.execution.records import ToolPolicyDecision
from app.tools.execution.service import ToolExecutionService
from app.tools.concurrent import ToolConcurrentScheduler
from app.tools.executor import PreparedToolCall, ToolCallExecutor
from app.tools.registry import ToolRegistry
from app.tools.results import ToolObservationBuilder, ToolRuntimeResult
from app.tools.schema import validate_tool_arguments
from app.tools.types import ToolCall, ToolDefinition, ToolObservation


@dataclass(frozen=True)
class ToolExecutionContext:
    """携带工具调用关联运行事实的上下文。

    参数:
        run_id: Durable Run 标识；兼容调用可为空。
        step_id: 可选运行时步骤标识。

    返回:
        不可变执行上下文。

    异常:
        无。

    副作用:
        无。
    """

    run_id: str = ""
    step_id: Optional[str] = None


class ToolRuntime:
    """编排工具校验、策略、审批、持久化和并发执行。"""

    def __init__(
        self,
        registry: ToolRegistry,
        executor: ToolCallExecutor,
        observation_builder: ToolObservationBuilder,
        logger: logging.Logger,
        execution_service: Optional[ToolExecutionService] = None,
        approval_service: Optional[ApprovalService] = None,
        idempotency_keys: Optional[ToolIdempotencyKeyBuilder] = None,
        concurrency_planner: Optional[ToolConcurrencyPlanner] = None,
        concurrent_scheduler: Optional[ToolConcurrentScheduler] = None,
        compatibility_policy: Optional[Callable[[ToolDefinition], ToolPolicyDecision]] = None,
    ) -> None:
        """初始化 Tool Runtime 依赖。

        参数:
            registry: handler 和工具定义的唯一注册入口。
            executor: 只负责单个 handler 执行的执行器。
            observation_builder: 归一化模型观测构建器。
            logger: 写入工具生命周期诊断的日志器。
            execution_service: 可选工具调用事实服务。
            approval_service: 可选 Durable Run 审批服务。
            idempotency_keys: 可选幂等键构造器。
            concurrency_planner: 可选并发计划构造器。
            concurrent_scheduler: 可选并发调度器。
            compatibility_policy: 未接入持久化服务时使用的兼容策略回调。

        返回:
            无。

        异常:
            无。

        副作用:
            保存平台依赖。
        """

        self._registry = registry
        self._executor = executor
        self._observation_builder = observation_builder
        self._logger = logger
        self._execution_service = execution_service
        self._approval_service = approval_service
        self._idempotency_keys = idempotency_keys or ToolIdempotencyKeyBuilder()
        self._concurrency_planner = concurrency_planner or ToolConcurrencyPlanner()
        self._concurrent_scheduler = concurrent_scheduler
        self._compatibility_policy = compatibility_policy

    def list_model_visible_tools(self) -> list[ToolDefinition]:
        """返回由注册表和当前策略共同允许模型看见的工具。

        参数:
            无。

        返回:
            排序后的模型可见工具定义。

        异常:
            无。

        副作用:
            无。
        """

        return [tool for tool in self._registry.list_tools() if tool.visible_by_default]

    def execute_single_tool_call(
        self,
        call: ToolCall,
        context: Optional[ToolExecutionContext] = None,
    ) -> ToolObservation:
        """执行单个工具调用的完整生命周期。

        参数:
            call: 模型或兼容入口请求的工具调用。
            context: 可选 Durable Run 和步骤关联上下文。

        返回:
            成功、失败或待审批的归一化工具观测。

        异常:
            无。可预期的平台异常转换为观测结果。

        副作用:
            可能创建工具调用记录、审批请求、artifact 和 handler 副作用。
        """

        prepared_or_observation = self.prepare_tool_call(call, context or ToolExecutionContext())
        if isinstance(prepared_or_observation, ToolObservation):
            return prepared_or_observation
        return self._executor.execute(prepared_or_observation).observation

    def execute_tool_calls(
        self,
        calls: Sequence[ToolCall],
        context: Optional[ToolExecutionContext] = None,
    ) -> list[ToolObservation]:
        """按资源冲突规则执行同一模型响应中的多个工具调用。

        参数:
            calls: 需要执行的工具调用序列。
            context: 共享 Durable Run 与步骤上下文。

        返回:
            与输入调用顺序一致的观测结果列表。

        异常:
            无。单调用失败被隔离为对应错误观测。

        副作用:
            可能并发执行多个 handler，并写入其各自执行事实。
        """

        execution_context = context or ToolExecutionContext()
        prepared: list[PreparedToolCall] = []
        immediate: dict[int, ToolObservation] = {}
        for index, call in enumerate(calls):
            candidate = self.prepare_tool_call(call, execution_context)
            if isinstance(candidate, ToolObservation):
                immediate[index] = candidate
            else:
                prepared.append(candidate)
        if not prepared:
            return [immediate[index] for index in range(len(calls))]
        plan = self._concurrency_planner.plan(
            [ToolCall(item.tool.name, item.arguments, item.tool_call_id) for item in prepared],
            self._registry.list_tools(),
        )
        prepared_groups = self._prepared_groups(plan, prepared)
        if self._concurrent_scheduler is None:
            completed = [self._executor.execute(item) for item in prepared]
        else:
            completed = self._concurrent_scheduler.run(plan, prepared_groups, self._executor.execute)
        completed_observations = iter(result.observation for result in completed)
        result: list[ToolObservation] = []
        for index, call in enumerate(calls):
            if index in immediate:
                result.append(immediate[index])
            else:
                result.append(next(completed_observations))
        return result

    def resume_approved_tool_call(
        self,
        call: ToolCall,
        context: ToolExecutionContext,
        approval_id: str,
    ) -> ToolObservation:
        """只在持久化审批已批准后恢复一个等待中的工具调用。

        参数:
            call: 与原始审批请求一致的工具调用。
            context: 原始 Durable Run 和步骤上下文。
            approval_id: 已提交审批决策的审批请求标识。

        返回:
            恢复执行后的成功或失败观测；未批准时返回错误观测。

        异常:
            RuntimeError: 当 Runtime 未配置审批或执行事实服务时抛出。
            KeyError: 当审批请求不存在时抛出。

        副作用:
            校验审批、推进生命周期并可能执行一次 handler 副作用。
        """

        if self._approval_service is None or self._execution_service is None:
            raise RuntimeError("approval resume requires approval and execution services")
        approval = self._approval_service.get_request(approval_id)
        decision = self._approval_service.get_decision(approval_id)
        if approval.run_id != context.run_id or approval.tool_call_id is None:
            return self._observation_builder.error(call.tool_name, "approval does not match tool execution context")
        if decision is None or decision.decision != "approved":
            if decision is not None and decision.decision == "denied":
                self._execution_service.mark_cancelled(approval.tool_call_id)
            return self._observation_builder.error(
                call.tool_name,
                "tool approval was not granted",
                approval.permission,
                "deny" if decision is not None else "pending",
                approval.tool_call_id,
            )
        try:
            tool = self._registry.get(call.tool_name)
        except KeyError:
            return self._observation_builder.error(call.tool_name, f"unknown tool: {call.tool_name}")
        arguments = self._validate_arguments(tool, call)
        if isinstance(arguments, ToolObservation):
            return arguments
        if approval.tool_name != tool.name or dict(arguments) != approval.payload.get("arguments"):
            return self._observation_builder.error(tool.name, "approval payload does not match tool call", tool.permission)
        existing = self._execution_service.get_call_by_key(
            self._idempotency_keys.build(context.run_id, context.step_id, tool.name, arguments)
        )
        if existing is not None and existing.status == "completed":
            self._logger.info(
                "tool_approval_resume_reused run_id=%s step_id=%s tool_call_id=%s approval_id=%s",
                context.run_id,
                context.step_id,
                existing.tool_call_id,
                approval_id,
            )
            return self._observation_builder.success(
                tool.name,
                "approved tool call already completed; side effect was not repeated",
                tool.permission,
                existing.tool_call_id,
            )
        record = self._execution_service.mark_approved(approval.tool_call_id)
        prepared = PreparedToolCall(
            tool,
            arguments,
            context.run_id,
            context.step_id,
            record.tool_call_id,
            self._idempotency_keys.build(context.run_id, context.step_id, tool.name, arguments, approval_id),
        )
        return self._executor.execute(prepared).observation

    def prepare_tool_call(
        self,
        call: ToolCall,
        context: ToolExecutionContext,
    ) -> PreparedToolCall | ToolObservation:
        """完成 handler 执行前的查找、校验、策略和审批准备。

        参数:
            call: 原始工具调用。
            context: Durable Run 与步骤关联上下文。

        返回:
            可交给 ToolCallExecutor 的已准备调用，或无需执行的观测结果。

        异常:
            无。未知工具、参数错误和策略拒绝均转换为观测结果。

        副作用:
            在配置执行服务时持久化调用；审批场景会创建 Durable Run 等待点。
        """

        try:
            tool = self._registry.get(call.tool_name)
        except KeyError:
            self._logger.warning("tool_missing run_id=%s tool=%s", context.run_id, call.tool_name)
            return self._observation_builder.error(call.tool_name, f"unknown tool: {call.tool_name}")
        arguments = self._validate_arguments(tool, call)
        if isinstance(arguments, ToolObservation):
            return arguments
        idempotency_key = self._idempotency_keys.build(context.run_id, context.step_id, tool.name, arguments)
        tool_call_id = call.call_id
        decision = self._compatibility_decision(tool)
        if self._execution_service is not None:
            existing = self._execution_service.get_call_by_key(idempotency_key)
            record, decision = self._execution_service.plan_tool_call(
                run_id=context.run_id,
                tool_name=tool.name,
                arguments=dict(arguments),
                permission=tool.permission,
                idempotency_key=idempotency_key,
                step_id=context.step_id,
            )
            tool_call_id = record.tool_call_id
            if existing is not None and record.status == "waiting_approval":
                return ToolObservation(
                    tool_name=tool.name,
                    status="approval_required",
                    content="",
                    error="tool approval is already pending",
                    permission=tool.permission,
                    approval_status="approval_required",
                    tool_call_id=tool_call_id,
                )
            if record.status == "completed":
                self._logger.info(
                    "tool_idempotency_reused run_id=%s step_id=%s tool_call_id=%s tool=%s idempotency_key=%s",
                    context.run_id,
                    context.step_id,
                    tool_call_id,
                    tool.name,
                    idempotency_key,
                )
                return self._observation_builder.success(
                    tool.name,
                    "tool call already completed; side effect was not repeated",
                    tool.permission,
                    tool_call_id,
                )
        if decision.status == "deny":
            self._logger.warning("tool_policy_denied run_id=%s step_id=%s tool_call_id=%s tool=%s idempotency_key=%s", context.run_id, context.step_id, tool_call_id, tool.name, idempotency_key)
            return self._observation_builder.error(tool.name, decision.reason, tool.permission, "deny", tool_call_id)
        if decision.status == "approval_required":
            return self._request_approval(tool, arguments, context, tool_call_id, decision)
        return PreparedToolCall(tool, arguments, context.run_id, context.step_id, tool_call_id, idempotency_key)

    def _compatibility_decision(self, tool: ToolDefinition) -> ToolPolicyDecision:
        """为未接入执行事实服务的兼容调用生成策略决策。

        参数:
            tool: 已注册工具定义。

        返回:
            Tool v2 统一格式的策略决策。

        异常:
            无。

        副作用:
            无。
        """

        if self._compatibility_policy is None:
            return ToolPolicyDecision("allow", tool.risk_level, "compatibility execution", tool.permission)
        return self._compatibility_policy(tool)

    def _validate_arguments(self, tool: ToolDefinition, call: ToolCall) -> Mapping[str, object] | ToolObservation:
        """校验对象形参数量、必填字段和 JSON Schema。

        参数:
            tool: 被请求工具定义。
            call: 原始工具调用。

        返回:
            已验证参数映射或错误观测。

        异常:
            无。

        副作用:
            无。
        """

        if not isinstance(call.arguments, Mapping):
            return self._observation_builder.error(tool.name, "invalid tool arguments: expected object", tool.permission)
        missing = [name for name in tool.required_params if name not in call.arguments]
        if missing:
            return self._observation_builder.error(tool.name, f"missing required parameters: {', '.join(missing)}", tool.permission)
        schema_error = validate_tool_arguments(call.arguments, tool.parameters_schema)
        if schema_error:
            return self._observation_builder.error(tool.name, f"invalid tool arguments: {schema_error}", tool.permission)
        return call.arguments

    def _request_approval(
        self,
        tool: ToolDefinition,
        arguments: Mapping[str, object],
        context: ToolExecutionContext,
        tool_call_id: str,
        decision: ToolPolicyDecision,
    ) -> ToolObservation:
        """持久化审批请求并返回不执行副作用的等待观测。

        参数:
            tool: 已验证工具定义。
            arguments: 已验证工具参数。
            context: Durable Run 与步骤上下文。
            tool_call_id: 持久化工具调用标识。
            decision: 要求审批的策略结果。

        返回:
            ``approval_required`` 状态观测。

        异常:
            RuntimeError: 当生产调用缺少审批服务或 run 标识时抛出。

        副作用:
            创建审批请求并将 Durable Run 标为 waiting。
        """

        if self._approval_service is None or not context.run_id:
            return ToolObservation(
                tool_name=tool.name,
                status="approval_required",
                content="",
                error=decision.reason,
                permission=tool.permission,
                approval_status="approval_required",
                tool_call_id=tool_call_id,
            )
        approval = self._approval_service.request_approval(
            run_id=context.run_id,
            tool_name=tool.name,
            permission=tool.permission,
            risk_level=decision.risk_level,
            payload={"arguments": dict(arguments), "tool_name": tool.name},
            step_id=context.step_id,
            tool_call_id=tool_call_id,
        )
        self._logger.info("tool_approval_requested run_id=%s step_id=%s tool_call_id=%s approval_id=%s tool=%s", context.run_id, context.step_id, tool_call_id, approval.approval_id, tool.name)
        return ToolObservation(tool.name, "approval_required", "", decision.reason, tool.permission, "approval_required", tool_call_id)

    def _prepared_groups(self, plan, prepared: Sequence[PreparedToolCall]) -> list[list[PreparedToolCall]]:
        """将并发计划中的调用映射为已准备调用。

        参数:
            plan: 已生成的并发计划。
            prepared: 已通过校验和策略的调用。

        返回:
            与计划一一对应的已准备调用分组。

        异常:
            KeyError: 当计划引用未知准备调用时抛出。

        副作用:
            无。
        """

        by_id = {item.tool_call_id: item for item in prepared}
        return [[by_id[call.call_id] for call in group.calls] for group in plan.groups]
