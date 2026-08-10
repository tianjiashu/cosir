"""Runtime-side executor for delegate_task tool calls."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.delegation.child_agent_profile_builder import ChildAgentProfileBuilder
from app.models import TaskRecord, TurnRecord
from app.service.delegation.delegation_context import DelegationPolicyContext
from app.service.delegation.delegation_policy import DelegationPolicy
from app.service.delegation.delegation_result import DelegationResult
from app.service.delegation.delegation_service import DelegationService
from app.service.task.turn_service import TurnService
from app.tools.schemas import ToolExecutionContext, ToolObservation
from app.tools.tool_execute.tool_error import tool_error
from app.tools.tool_execute.tool_success import tool_success
from app.tools.tool_models.delegate_task_args import DelegateTaskArgs


class DelegationExecutor:
    """Execute validated delegate_task requests through existing child Agent runtime paths."""

    def __init__(
        self,
        agent_registry: AgentProfileRegistry,
        delegation_service: DelegationService,
        turn_service: TurnService,
        child_runner: Any,
        parent_profile: AgentProfile,
        parent_turn: TurnRecord,
        parent_task: TaskRecord,
        registered_tool_names: Iterable[str],
        execution_context: ToolExecutionContext | None = None,
        policy: DelegationPolicy | None = None,
    ) -> None:
        """初始化委派执行器。

        参数:
            agent_registry: 现有 AgentProfileRegistry，用于解析 child profile 模板。
            delegation_service: 委派生命周期 service。
            turn_service: 现有 TurnService，用于创建和认领 child turn。
            child_runner: 运行 child AgentProfile 的协作者，需提供 ``run_child``。
            parent_profile: 当前父 turn 使用的 AgentProfile。
            parent_turn: 当前父 turn 记录。
            parent_task: 当前父 task 记录。
            registered_tool_names: 当前工具注册表中已注册工具名称。
            execution_context: 可选测试兼容字段；实际执行以 execute 入参为准。
            policy: 可选委派策略实例；缺省使用 DelegationPolicy。

        返回:
            无。

        异常:
            无。

        副作用:
            保存运行时协作者引用。
        """

        self._agent_registry = agent_registry
        self._delegation_service = delegation_service
        self._turn_service = turn_service
        self._child_runner = child_runner
        self._parent_profile = parent_profile
        self._parent_turn = parent_turn
        self._parent_task = parent_task
        self._registered_tool_names = tuple(registered_tool_names)
        self._execution_context = execution_context
        self._policy = policy or DelegationPolicy()

    def execute(
        self,
        args: DelegateTaskArgs,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """执行一次委派请求并返回父工具 observation。

        参数:
            args: 已校验的 delegate_task 工具参数。
            execution_context: 父工具执行上下文；用于确认 parent turn 边界。

        返回:
            child 成功时返回 success observation；策略拒绝、child 失败、取消或异常时返回
            deterministic error observation。

        异常:
            无。已创建 delegation 后的运行异常会转换为 failed delegation 和工具错误。

        副作用:
            可能创建 delegation 记录、child turn，运行 child Agent，并更新 delegation 终态。
        """

        child_template = self._agent_registry.resolve(args.child_agent_id)
        decision = self._policy.resolve(
            DelegationPolicyContext(
                parent_agent_id=self._parent_profile.agent_id,
                child_agent_id=args.child_agent_id,
                requested_tools=tuple(args.requested_tools),
                parent_allowed_tools=frozenset(self._parent_profile.allowed_tools),
                child_allowed_tools=frozenset(
                    child_template.allowed_tools if child_template else ()
                ),
                system_allowed_tools=frozenset(self._system_allowed_tools()),
                depth=1 if self._parent_turn.parent_turn_id else 0,
                running_children=self._delegation_service.count_active_children(
                    self._parent_turn.turn_id
                ),
                known_child_agent_ids=frozenset(self._agent_registry.list_agent_ids()),
            )
        )
        if not decision.allowed or child_template is None:
            return self._policy_error(args.child_agent_id, decision.reason)

        delegation_id = ""
        try:
            delegation_id = self._delegation_service.create_pending(
                task_id=self._parent_task.task_id,
                parent_turn_id=self._parent_turn.turn_id,
                parent_agent_id=self._parent_profile.agent_id,
                child_agent_id=args.child_agent_id,
                delegation_type=args.delegation_type,
                prompt=args.prompt,
                requested_tools=tuple(args.requested_tools),
                effective_tools=decision.effective_tools,
            )
            child_turn = self._turn_service.create_child_turn(
                task_id=self._parent_task.task_id,
                input_text=args.prompt,
                agent_id=args.child_agent_id,
                parent_turn_id=self._parent_turn.turn_id,
                delegation_id=delegation_id,
            )
            if not self._turn_service.claim_pending_turn(child_turn.turn_id):
                raise RuntimeError("child_turn_claim_lost")
            self._delegation_service.mark_child_started(delegation_id, child_turn.turn_id)
            child_profile = ChildAgentProfileBuilder.build(
                registry_profile=child_template,
                turn=child_turn,
                effective_tools=decision.effective_tools,
            )
            result = self._child_runner.run_child(child_profile)
        except Exception as exc:
            log.exception(
                "delegation_execution_failed",
                extra={
                    "msg": "委派执行异常，已转换为 delegate_task 工具错误",
                    "data": {
                        "delegation_id": delegation_id,
                        "parent_turn_id": self._parent_turn.turn_id,
                        "child_agent_id": args.child_agent_id,
                    },
                },
            )
            if delegation_id:
                self._delegation_service.mark_failed(delegation_id, str(exc))
            return self._child_error("failed", str(exc))

        return self._finalize_result(delegation_id, result)

    def _system_allowed_tools(self) -> tuple[str, ...]:
        """返回系统策略允许 child 使用的工具名称。

        参数:
            无。

        返回:
            当前已注册工具名称中剔除 ``delegate_task`` 后的元组。

        异常:
            无。

        副作用:
            无。
        """

        return tuple(
            tool_name for tool_name in self._registered_tool_names if tool_name != "delegate_task"
        )

    def _policy_error(self, child_agent_id: str, reason: str) -> ToolObservation:
        """构造策略拒绝的工具错误 observation。

        参数:
            child_agent_id: 被拒绝的 child Agent 标识。
            reason: 策略拒绝原因短码。

        返回:
            delegate_task error observation。

        异常:
            无。

        副作用:
            写入策略拒绝日志。
        """

        log.info(
            "delegation_policy_denied",
            extra={
                "msg": "委派请求被策略拒绝",
                "data": {
                    "parent_turn_id": self._parent_turn.turn_id,
                    "child_agent_id": child_agent_id,
                    "reason": reason,
                },
            },
        )
        return tool_error(
            "delegate_task",
            f"delegate_task denied: {reason}",
            reason=(
                f"the delegation request was denied by policy with reason "
                f"'{reason}'; this is deterministic, so adjust the child agent, "
                f"requested tools, depth, or active child count before retrying."
            ),
            permission="delegate_task",
        )

    def _finalize_result(self, delegation_id: str, result: DelegationResult) -> ToolObservation:
        """根据 child 终态更新 delegation 并返回父工具 observation。

        参数:
            delegation_id: 当前 delegation 标识。
            result: child runner 返回的终态结果。

        返回:
            success 或 error ToolObservation。

        异常:
            KeyError: 如果 delegation 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果终态持久化失败。

        副作用:
            更新 delegation 终态并发出对应 runtime event。
        """

        if result.status == "completed":
            summary = result.summary or "child turn completed"
            self._delegation_service.mark_completed(delegation_id, summary)
            return tool_success(
                "delegate_task",
                "delegate_task",
                summary,
                data={
                    "delegation_id": delegation_id,
                    "child_turn_id": result.child_turn_id,
                    "status": "completed",
                },
            )
        if result.status == "cancelled":
            error = result.error or "child turn cancelled"
            self._delegation_service.mark_cancelled(delegation_id, error)
            return self._child_error("cancelled", error)
        error = result.error or "child turn failed"
        self._delegation_service.mark_failed(delegation_id, error)
        return self._child_error("failed", error)

    def _child_error(self, status: str, error: str) -> ToolObservation:
        """构造 child 失败或取消对应的工具错误 observation。

        参数:
            status: child 委派终态。
            error: child 失败或取消原因。

        返回:
            delegate_task error observation。

        异常:
            无。

        副作用:
            无。
        """

        return tool_error(
            "delegate_task",
            f"delegate_task child {status}: {error}",
            reason=(
                f"the delegated child agent ended with status '{status}': {error}. "
                f"Treat this delegate_task call as terminal and continue from the "
                f"reported child result instead of retrying identical arguments."
            ),
            permission="delegate_task",
        )
