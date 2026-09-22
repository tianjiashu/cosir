"""Runtime-side executor for delegate_task tool calls."""

from __future__ import annotations

import json
from typing import Any

from app.assistant_transport.event import DelegationRefData, ToolCallRuntimeUpdateEvent
from app.config.configuration import get_agent_registry
from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.core.tools.display.delegation_display import build_delegation_display_data
from app.core.tools.schemas import ToolExecutionContext, ToolObservation
from app.core.tools.schemas.delegate_task_executor import DelegateTaskExecutor
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_models import DelegateTaskArgs
from app.models import ConversationRunRecord, TaskRecord
from app.service.child_agent.child_agent_session_error import ChildAgentSessionError
from app.service.delegation.delegation_context import DelegationPolicyDecision
from app.service.depends import get_child_agent_session_service


class DelegationExecutor(DelegateTaskExecutor):
    """Production runtime implementation of the delegate_task execution port."""

    def __init__(
        self,
        child_runner: Any,
        parent_profile: AgentProfile,
        parent_run: ConversationRunRecord,
        parent_task: TaskRecord,
    ) -> None:
        """初始化委派执行器。

        参数:
            child_runner: 运行 child AgentProfile 的协作者，需提供 ``run_child``。
            parent_profile: 当前父 run 使用的 AgentProfile。
            parent_run: 当前父 Conversation Run 记录。
            parent_task: 当前父 task 记录。

        返回:
            无。

        异常:
            无。

        副作用:
            保存当前 run 的执行上下文引用。
        """

        self._child_runner = child_runner
        self._parent_profile = parent_profile
        self._parent_run = parent_run
        self._parent_task = parent_task

    def execute(
        self,
        args: DelegateTaskArgs,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """执行一次委派请求并返回父工具 observation。

        参数:
            args: 已校验的 delegate_task 工具参数（child_agent_id / title / prompt 自由文本契约）。
            execution_context: 父工具执行上下文；用于确认 parent run 边界。

        返回:
            child 成功时返回 success observation；child 无法解析、策略拒绝、child 失败、取消
            或异常时返回 deterministic error observation。

        异常:
            无。已创建 delegation 后的运行异常会转换为 failed delegation 和工具错误。

        副作用:
            可能创建 delegation 记录、child run，运行 child Agent，并更新 delegation 终态。
        """

        runtime_event_loop = execution_context.runtime_dependencies.runtime_event_loop
        agent_registry = get_agent_registry()

        # 获取child agent profile
        child_agent_profile: AgentProfile | None = agent_registry.resolve(args.child_agent_id)
        if child_agent_profile is None:
            log.warning(
                "delegate_child_resolve_failed",
                extra={
                    "msg": "委派目标 child Agent 无法解析，返回工具错误",
                    "data": {
                        "parent_run_id": self._parent_run.id,
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

        # 校验执行策略（深度、已知 child Agent、工具收敛三类）
        decision = self._resolve_delegation(
            child_agent_id=args.child_agent_id,
            child_allowed_tools=frozenset(child_agent_profile.allowed_tools),
            # depth 语义：发起者所在 task 已处的委派层数——主 Agent 顶层 task
            # 为 0（允许发起第一层委派），委派子 task 为 1（拒绝递归委派）。
            # 取自 TaskRecord 持久化事实而非 profile 运行时字段，避免共享
            # profile 实例被并发 run 改写导致 depth 误判。
            depth=1 if self._parent_task.is_child else 0,
            known_child_agent_ids=frozenset(agent_registry.child_agent_ids()),
        )
        if not decision.allowed:
            return self._policy_error(args.child_agent_id, decision.reason)

        # Child Agent sessions are canonical Task/Run facts plus a process-local runtime
        # registry.  The synchronous tool handler waits only for launch registration;
        # child workflow completion is intentionally owned by ConversationRunExecutor.
        session_service = get_child_agent_session_service()
        if runtime_event_loop is None:
            return tool_error(
                "delegate_task",
                "child_agent_start_failed",
                reason="The parent runtime event loop is unavailable; retry the delegation.",
                permission="delegate_task",
            )

        async def run_child_workflow(child_run: ConversationRunRecord) -> None:
            child_profile = child_agent_profile.derive_for_run(
                child_run,
                allowed_tools=list(decision.effective_tools),
                runtime_event_loop=runtime_event_loop,
                model_defaults=self._parent_profile,
            )
            await self._child_runner.run_child_workflow(child_profile)

        try:
            started = session_service.start_child(
                parent_task_id=self._parent_task.id,
                parent_run_id=self._parent_run.id,
                workspace_id=self._parent_task.workspace_id,
                child_agent_id=args.child_agent_id,
                title=args.title,
                prompt=args.prompt,
                tool_call_id=execution_context.tool_call_id,
                run_callback=run_child_workflow,
                runtime_event_loop=runtime_event_loop,
                provider_id=self._resolve_child_model_config(child_agent_profile)[0],
                model_name=self._resolve_child_model_config(child_agent_profile)[1],
                reasoning_effort=self._resolve_child_model_config(child_agent_profile)[2],
            )
        except ChildAgentSessionError as exc:
            return tool_error(
                "delegate_task",
                exc.code,
                reason=f"Child Agent startup was rejected: {exc.code}.",
                permission="delegate_task",
            )
        except Exception as exc:
            log.exception(
                "child_agent_start_failed",
                extra={
                    "msg": "Child Agent session 创建失败",
                    "data": {
                        "parent_run_id": self._parent_run.id,
                        "child_agent_id": args.child_agent_id,
                        "error_type": type(exc).__name__,
                    },
                },
            )
            return tool_error(
                "delegate_task",
                "child_agent_start_failed",
                reason=(
                    "The Child Agent could not be started; inspect the backend log and retry later."
                ),
                permission="delegate_task",
            )

        try:
            if execution_context.tool_call_id:
                from app.assistant_transport.event.dispatch import dispatch_conversation_event

                dispatch_conversation_event(
                    ToolCallRuntimeUpdateEvent(
                        task_id=self._parent_task.id,
                        run_id=self._parent_run.id,
                        tool_call_id=execution_context.tool_call_id,
                        seq=0,
                        data=DelegationRefData(
                            kind="delegation_ref",
                            child_task_id=started.child_task_id,
                            child_run_id=started.child_run_id,
                            title=args.title,
                            role=child_agent_profile.role,
                        ),
                    )
                )
        except Exception:
            log.exception(
                "delegation_ref_event_failed",
                extra={
                    "msg": "委派引用事件投影失败，继续执行 child Agent",
                    "data": {
                        "parent_run_id": self._parent_run.id,
                        "child_task_id": started.child_task_id,
                        "child_run_id": started.child_run_id,
                    },
                },
            )
        return tool_success(
            "delegate_task",
            "delegate_task",
            json.dumps(
                {
                    "status": "started",
                    "child_task_id": started.child_task_id,
                    "child_run_id": started.child_run_id,
                    "child_agent_id": args.child_agent_id,
                },
                separators=(",", ":"),
            ),
            display_data=build_delegation_display_data(
                title=args.title,
                child_agent_id=args.child_agent_id,
                child_task_id=started.child_task_id,
                child_run_id=started.child_run_id,
                status="running",
                role=child_agent_profile.role,
            ),
        )

    def _resolve_child_model_config(
        self,
        child_profile: AgentProfile,
    ) -> tuple[int | None, str | None, str | None]:
        """解析 child Run 的模型路由，默认继承当前父 Run 的有效配置。

        参数:
            child_profile: 注册表中的 child Agent Profile。其显式 provider/model 配置
                优先于父 Run，允许未来为特定 child 定制模型；未配置的字段从父 Run 继承。

        返回:
            ``(provider_id, model_name, reasoning_effort)``，可直接传入 child Run 创建
            service。provider/model 未显式定制时读取父 Run；reasoning_effort 也遵循同样
            的覆盖规则，未定制时才继承父 Run 的持久化选择。

        异常:
            无。

        副作用:
            无。仅读取 profile 和父 Run，不创建数据库记录。
        """

        return (
            child_profile.provider_id
            if child_profile.provider_id is not None
            else self._parent_run.provider_id,
            child_profile.model_name
            if child_profile.model_name is not None
            else self._parent_run.model_name,
            child_profile.model_settings.reasoning_effort
            if child_profile.model_settings.reasoning_effort is not None
            else self._parent_run.reasoning_effort,
        )

    @staticmethod
    def _resolve_delegation(
        child_agent_id: str,
        child_allowed_tools: frozenset[str],
        depth: int,
        known_child_agent_ids: frozenset[str],
        max_depth: int = 1,
    ) -> DelegationPolicyDecision:
        """依据深度、已知 Agent 与工具收敛三类规则裁决单次委派请求。

        本方法承接原 ``DelegationPolicy.resolve`` 的职责，作为 ``DelegationExecutor``
        的纯函数式策略裁决，不持有任何实例状态。有效工具完全由 child 工具权限收敛决定，
        父 Agent 与系统级工具集合不参与计算；并发额度（``max_concurrency``）的裁决已
        下沉到 storage 层原子 acquire，本方法只负责以下三类校验：

        * 已知性：``child_agent_id`` 必须存在于 ``known_child_agent_ids``，否则拒绝
          （``unknown_child_agent``）。
        * 深度：``depth >= max_depth`` 时拒绝（``delegation_depth_exceeded``），默认
          最大深度为 1，即仅允许主 Agent 发起一层委派，禁止递归委派。
        * 工具收敛：``child_allowed_tools`` 剔除 ``delegate_task``（避免 child 递归委派）
          后若为空则拒绝（``no_effective_tools``）。

        参数:
            child_agent_id: 目标 child Agent 标识。
            child_allowed_tools: child Agent profile 声明的可用工具集合。
            depth: 发起者所在 task 已处的委派层数（主 Agent 顶层为 0，委派子 task 为 1）。
            known_child_agent_ids: 注册表中已知 child Agent 标识集合。
            max_depth: 委派链最大允许深度，默认 1。

        返回:
            包含是否允许、拒绝原因和生效工具列表的策略决策。生效工具为
            ``child_allowed_tools`` 剔除 ``delegate_task`` 后的集合，按字典序排序后转
            tuple，保证跨运行确定性。

        异常:
            无。

        副作用:
            无。
        """

        if child_agent_id not in known_child_agent_ids:
            return DelegationPolicyDecision(False, "unknown_child_agent", ())
        if depth >= max_depth:
            return DelegationPolicyDecision(False, "delegation_depth_exceeded", ())
        effective_tools = child_allowed_tools - frozenset({"delegate_task"})
        if not effective_tools:
            return DelegationPolicyDecision(False, "no_effective_tools", ())
        return DelegationPolicyDecision(True, "", tuple(sorted(effective_tools)))

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
                    "parent_run_id": self._parent_run.id,
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
