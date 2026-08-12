"""Runtime-side executor for delegate_task tool calls."""

from __future__ import annotations

import asyncio
from typing import Any

from app.config.configuration import get_agent_registry
from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.agents.agent_profile import AgentProfile
from app.core.delegation.child_agent_profile_builder import ChildAgentProfileBuilder
from app.models import TaskRecord, TurnRecord
from app.service.delegation.delegation_context import DelegationPolicyContext
from app.service.delegation.delegation_policy import DelegationPolicy
from app.service.delegation.delegation_result import DelegationResult
from app.service.delegation.delegation_service import DelegationService
from app.service.depends import get_delegation_service, get_turn_service
from app.tools.schemas import ToolExecutionContext, ToolObservation
from app.tools.schemas.delegate_task_executor import DelegateTaskExecutor
from app.tools.tool_execute.tool_error import tool_error
from app.tools.tool_execute.tool_success import tool_success
from app.tools.tool_models.delegate_task_args import DelegateTaskArgs


class DelegationExecutor(DelegateTaskExecutor):
    """Production runtime implementation of the delegate_task execution port."""

    def __init__(
            self,
            child_runner: Any,
            parent_profile: AgentProfile,
            parent_turn: TurnRecord,
            parent_task: TaskRecord,
            policy: DelegationPolicy | None = None,
    ) -> None:
        """初始化委派执行器。

        参数:
            child_runner: 运行 child AgentProfile 的协作者，需提供 ``run_child``。
            parent_profile: 当前父 turn 使用的 AgentProfile。
            parent_turn: 当前父 turn 记录。
            parent_task: 当前父 task 记录。
            policy: 可选委派策略实例；缺省使用 DelegationPolicy。

        返回:
            无。

        异常:
            无。

        副作用:
            保存当前 turn 的执行上下文引用。
        """

        self._child_runner = child_runner
        self._parent_profile = parent_profile
        self._parent_turn = parent_turn
        self._parent_task = parent_task
        self._policy = policy or DelegationPolicy()

    def execute(
            self,
            args: DelegateTaskArgs,
            execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """执行一次委派请求并返回父工具 observation。

        参数:
            args: 已校验的 delegate_task 工具参数（结构化 objective / rules / references /
                expected_output）。
            execution_context: 父工具执行上下文；用于确认 parent turn 边界。

        返回:
            child 成功时返回 success observation；child 无法解析、策略拒绝、child 失败、取消
            或异常时返回 deterministic error observation。

        异常:
            无。已创建 delegation 后的运行异常会转换为 failed delegation 和工具错误。

        副作用:
            可能创建 delegation 记录、child turn，运行 child Agent，并更新 delegation 终态。
        """

        runtime_event_loop = execution_context.runtime_dependencies.runtime_event_loop
        agent_registry = get_agent_registry()
        delegation_service = get_delegation_service()
        turn_service = get_turn_service()

        # 获取child agent profile
        child_agent_profile: AgentProfile = agent_registry.resolve(args.child_agent_id)
        if child_agent_profile is None:
            log.warning(
                "delegate_child_resolve_failed",
                extra={
                    "msg": "委派目标 child Agent 无法解析，返回工具错误",
                    "data": {
                        "parent_turn_id": self._parent_turn.turn_id,
                        "child_agent_id": args.child_agent_id,
                    },
                },
            )
            return tool_error(
                "delegate_task",
                f"delegate_task child not found: {args.child_agent_id}",
                reason=(
                    f"the child agent '{args.child_agent_id}' is not registered; "
                    f"verify the child_agent_id against the available delegate_* agents "
                    f"before retrying."
                ),
                permission="delegate_task",
            )

        # 构建agent输入文本
        agent_input_text = self._build_agent_input_text(args)

        # 校验执行策略（深度、已知 child Agent、工具收敛三类）
        decision = self._policy.resolve(
            DelegationPolicyContext(
                parent_agent_id=self._parent_profile.agent_id,
                child_agent_id=args.child_agent_id,
                child_allowed_tools=frozenset(child_agent_profile.allowed_tools),
                depth=1 if self._parent_turn.parent_turn_id else 0,
                known_child_agent_ids=frozenset(agent_registry.child_agent_ids()),
            )
        )
        if not decision.allowed:
            return self._policy_error(args.child_agent_id, decision.reason)

        # 原子 acquire 并发额度：额度满时 storage 层在同一事务内拒绝创建
        acquire = delegation_service.try_create_pending(
            task_id=self._parent_task.task_id,
            parent_turn_id=self._parent_turn.turn_id,
            parent_agent_id=self._parent_profile.agent_id,
            child_agent_id=args.child_agent_id,
            delegation_type=self._delegation_type_from_child_agent_id(args.child_agent_id),
            prompt=agent_input_text,
            effective_tools=decision.effective_tools,
            max_concurrency=Settings.DELEGATION_MAX_CONCURRENCY,
            runtime_event_loop=runtime_event_loop,
        )
        if not acquire.acquired:
            return self._concurrency_exceeded_error(args.child_agent_id, acquire.reason)

        delegation_id = acquire.delegation_id
        try:
            # 创建pending child turn

            # 创建pending child turn
            child_turn = turn_service.create_child_turn(
                task_id=self._parent_task.task_id,
                input_text=agent_input_text,
                agent_id=args.child_agent_id,
                parent_turn_id=self._parent_turn.turn_id,
                delegation_id=delegation_id,
            )

            # 确认pending child turn
            if not turn_service.claim_pending_turn(child_turn.turn_id):
                raise RuntimeError("child_turn_claim_lost")

            # 标记child turn为已开始
            delegation_service.mark_child_started(
                delegation_id,
                child_turn.turn_id,
                runtime_event_loop=runtime_event_loop,
            )

            # 构建child agent profile
            child_profile = ChildAgentProfileBuilder.build(
                registry_profile=child_agent_profile,
                turn=child_turn,
                effective_tools=decision.effective_tools,
                context_excluded_turn_ids=(self._parent_turn.turn_id,),
                runtime_event_loop=runtime_event_loop,
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
                delegation_service.mark_failed(
                    delegation_id,
                    str(exc),
                    runtime_event_loop=runtime_event_loop,
                )
            return self._child_error("failed", str(exc))

        return self._finalize_result(
            delegation_id,
            result,
            runtime_event_loop,
            delegation_service,
        )

    def _build_agent_input_text(self, args: DelegateTaskArgs) -> str:
        """把结构化参数拼装为面向 child 的英文任务文本。

        拼装格式使用英文 section 标签（Objective / Rules / References / Background /
        Expected Output），完整保留父 Agent 传入的原文内容。空 rules / references /
        background 时省略对应 section，不输出空标题。

        参数:
            args: 已校验的 delegate_task 结构化参数。

        返回:
            可直接作为 child turn input_text 的结构化任务文本。

        异常:
            无。

        副作用:
            无。
        """

        title = args.title
        sections = [f"# {title}", "", "## Objective", args.objective]
        if args.rules:
            rules_block = "\n".join(f"- {rule}" for rule in args.rules)
            sections.extend(["", "## Rules", rules_block])
        if args.references:
            references_block = "\n".join(f"- {ref}" for ref in args.references)
            sections.extend(["", "## References", references_block])
        if args.background:
            sections.extend(["", "## Background", args.background])
        sections.extend(["", "## Expected Output", args.expected_output])
        return "\n".join(sections)

    def _delegation_type_from_child_agent_id(self, child_agent_id: str) -> str:
        """从 child Agent 标识派生稳定的委派类型标签。

        参数:
            child_agent_id: child Agent profile 的稳定标识。

        返回:
            去掉 ``delegate_`` 前缀后的类型；非约定前缀时原样返回。

        异常:
            无。

        副作用:
            无。
        """
        prefix = "delegate_"
        if child_agent_id.startswith(prefix):
            return child_agent_id[len(prefix):]
        return child_agent_id

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
                f"delegation depth, or active child count before retrying."
            ),
            permission="delegate_task",
        )

    def _concurrency_exceeded_error(
            self, child_agent_id: str, reason: str
    ) -> ToolObservation:
        """构造并发额度已满的工具错误 observation。

        并发额度由 storage 层在 ``try_create_pending`` 的原子事务内裁决，本方法仅在
        acquire 返回 ``acquired=False`` 时调用，把确定性拒绝转换为面向模型的可读错误。

        参数:
            child_agent_id: 被拒绝的 child Agent 标识。
            reason: 原子 acquire 返回的拒绝说明（英文富文本，含重试建议）。

        返回:
            delegate_task error observation（``retryable=False``）。

        异常:
            无。

        副作用:
            写入并发拒绝日志。
        """

        log.info(
            "delegation_concurrency_exceeded",
            extra={
                "msg": "委派被并发额度拒绝",
                "data": {
                    "parent_turn_id": self._parent_turn.turn_id,
                    "child_agent_id": child_agent_id,
                    "max_concurrency": Settings.DELEGATION_MAX_CONCURRENCY,
                },
            },
        )
        return tool_error(
            "delegate_task",
            f"delegate_task concurrency_exceeded: {child_agent_id}",
            reason=reason,
            permission="delegate_task",
        )

    def _finalize_result(
            self,
            delegation_id: str,
            result: DelegationResult,
            runtime_event_loop: asyncio.AbstractEventLoop | None,
            delegation_service: DelegationService,
    ) -> ToolObservation:
        """根据 child 终态更新 delegation 并返回父工具 observation。

        参数:
            delegation_id: 当前 delegation 标识。
            result: child runner 返回的终态结果。
            runtime_event_loop: 父运行时事件循环；用于线程安全发布 delegation 事件。
            delegation_service: 本次执行已解析出的委派生命周期 service。

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
            delegation_service.mark_completed(
                delegation_id,
                summary,
                runtime_event_loop=runtime_event_loop,
            )
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
            delegation_service.mark_cancelled(
                delegation_id,
                error,
                runtime_event_loop=runtime_event_loop,
            )
            return self._child_error("cancelled", error)
        error = result.error or "child turn failed"
        delegation_service.mark_failed(
            delegation_id,
            error,
            runtime_event_loop=runtime_event_loop,
        )
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
