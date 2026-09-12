"""Runtime-side executor for delegate_task tool calls."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.config.configuration import get_agent_registry
from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.agents.agent_profile import AgentProfile
from app.core.tools.display.delegation_display import build_delegation_display_data
from app.core.tools.schemas import ToolExecutionContext, ToolObservation
from app.core.tools.schemas.delegate_task_executor import DelegateTaskExecutor
from app.core.tools.tool_execute.tool_cancelled import tool_cancelled
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_models import DelegateTaskArgs
from app.models import ConversationRunRecord, TaskRecord
from app.models.result.delegation_result import DelegationResult
from app.service.delegation.delegation_context import DelegationPolicyDecision
from app.service.delegation.delegation_service import DelegationService
from app.service.depends import get_conversation_run_service, get_delegation_service


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

        from app.service.depends import get_task_service

        runtime_event_loop = execution_context.runtime_dependencies.runtime_event_loop
        agent_registry = get_agent_registry()
        delegation_service = get_delegation_service()
        conversation_run_state_service = get_conversation_run_service()
        task_service = get_task_service()

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

        # 构建agent输入文本
        agent_input_text = args.prompt

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

        # 原子 acquire 并发额度：额度满时 storage 层在同一事务内拒绝创建
        acquire = delegation_service.try_create_pending(
            task_id=self._parent_task.id,
            parent_run_id=self._parent_run.id,
            parent_agent_id=self._parent_profile.agent_id,
            child_agent_id=args.child_agent_id,
            prompt=agent_input_text,
            effective_tools=decision.effective_tools,
            max_concurrency=Settings.DELEGATION_MAX_CONCURRENCY,
            runtime_event_loop=runtime_event_loop,
        )
        if not acquire.acquired:
            return self._concurrency_exceeded_error(args.child_agent_id, acquire.reason)

        delegation_id = acquire.delegation_id
        try:
            # 先创建委派子任务（只建 task，不建 run；并发重入由 delegation_id 唯一索引兜底）。
            try:
                child_task = task_service.get_or_create_task(
                    task_id=None,
                    workspace_id=self._parent_task.workspace_id,
                    title=args.title,
                    task_type="delegation",
                    parent_task_id=self._parent_task.id,
                    parent_run_id=self._parent_run.id,
                    delegation_id=delegation_id,
                )
            except IntegrityError:
                # delegation_id 唯一索引冲突：同一 delegation 已被并发重入创建过子 task。
                # acquire 已插入 pending delegation 并占用 1 个并发额度，必须在此显式终态化，
                # 否则该 pending 记录永久计入 ACTIVE_DELEGATION_STATUSES，导致父 run 并发额度泄漏。
                log.error(
                    "delegation_child_task_conflict",
                    extra={
                        "msg": "委派子任务创建冲突（delegation_id 已存在子任务）",
                        "data": {
                            "delegation_id": delegation_id,
                            "parent_run_id": self._parent_run.id,
                        },
                    },
                )
                self._fail_delegation(
                    delegation_id,
                    delegation_service,
                    runtime_event_loop,
                    "child task already exists (concurrent re-entrancy)",
                )
                return tool_error(
                    "delegate_task",
                    f"delegate_task child task already exists: {delegation_id}",
                    reason=(
                        f"a child task for delegation '{delegation_id}' already exists; "
                        f"this delegation was already acquired and its child task created, "
                        f"so retrying identical arguments is deterministic and will fail "
                        f"again. Inspect the existing child task instead of re-delegating."
                    ),
                    retryable=False,
                    permission="delegate_task",
                )

            # 在子任务下创建 pending child run（上下文天然隔离，不依赖排除 hack）。
            # ① service 期预解析（设计 §6.4）：create_run 内部会把 child 请求的 /
            # 此处捕获后把 delegation 置 failed（child 无 HTTP 上下文，无法回 422），
            # 成对记 warn model_resolve_rejected(child=true) + error，父收失败 DelegationResult。
            child_provider_id, child_model_name, child_reasoning_effort = (
                self._resolve_child_model_config(child_agent_profile)
            )
            try:
                child_run = conversation_run_state_service.create_run(
                    task_id=child_task.id,
                    input_text=agent_input_text,
                    agent_id=args.child_agent_id,
                    provider_id=child_provider_id,
                    model_name=child_model_name,
                    reasoning_effort=child_reasoning_effort,
                )
            except Exception as exc:
                log.exception(
                    "child_delegation_model_resolve_failed",
                    extra={
                        "msg": f"委派 child 预解析模型失败，delegation 置 failed：{exc}",
                        "data": {
                            "delegation_id": delegation_id,
                            "parent_run_id": self._parent_run.id,
                            "child_agent_id": args.child_agent_id,
                        },
                    },
                )
                self._fail_delegation(
                    delegation_id,
                    delegation_service,
                    runtime_event_loop,
                    f"child model resolve failed: {exc}",
                )
                return self._child_error(
                    "failed",
                    f"child agent '{args.child_agent_id}' cannot run: model "
                    f"child model resolve failed: {exc}",
                )

            # 标记 child run 已进入执行提交阶段；pending→running 由统一 executor
            # 在取得 child task runtime space 后完成。
            delegation_service.mark_child_started(
                delegation_id,
                child_run.id,
                child_task_id=child_task.id,
                runtime_event_loop=runtime_event_loop,
            )

            # 构建child agent profile
            child_profile = child_agent_profile.derive_for_run(
                child_run,
                allowed_tools=list(decision.effective_tools),
                runtime_event_loop=runtime_event_loop,
                model_defaults=self._parent_profile,
            )
            result = self._child_runner.run_child(
                child_profile,
                delegation_id=delegation_id,
            )
        except Exception as exc:
            log.exception(
                "delegation_execution_failed",
                extra={
                    "msg": "委派执行异常，已转换为 delegate_task 工具错误",
                    "data": {
                        "delegation_id": delegation_id,
                        "parent_run_id": self._parent_run.id,
                        "child_agent_id": args.child_agent_id,
                    },
                },
            )
            self._fail_delegation(
                delegation_id,
                delegation_service,
                runtime_event_loop,
                str(exc),
            )
            return self._child_error("failed", str(exc))

        return self._finalize_result(
            delegation_id,
            result,
            runtime_event_loop,
            delegation_service,
            child_task_id=child_task.id,
            title=args.title,
            child_agent_id=args.child_agent_id,
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

    def _concurrency_exceeded_error(self, child_agent_id: str, reason: str) -> ToolObservation:
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
                    "parent_run_id": self._parent_run.id,
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

    def _fail_delegation(
        self,
        delegation_id: int | None,
        delegation_service: DelegationService,
        runtime_event_loop: asyncio.AbstractEventLoop | None,
        error: str,
    ) -> None:
        """把已 acquire 的 delegation 确定性终态化为 failed，释放并发额度。

        并发额度由 storage 层 ``try_create_pending`` 插入的 pending 记录承载：只要该记录
        仍处于 active（pending/running）状态，就会占用父 run 的并发槽。因此所有在 acquire
        成功后、未能通过 ``_finalize_result`` 正常终态化的提前退出路径（并发重入冲突、
        模型未配置、未预期异常）都必须调用本方法，把 delegation 推进到终态，避免额度泄漏。

        参数:
            delegation_id: 已 acquire 的 delegation 标识；为 None 时（acquire 失败）直接跳过。
            delegation_service: 本次执行已解析出的委派生命周期 service。
            runtime_event_loop: 父运行时事件循环；用于线程安全发布 delegation 事件。
            error: 失败原因（英文，面向日志与模型可读）。

        返回:
            无。

        异常:
            无；``mark_failed`` 自身异常被吞掉并记 log.error，避免二次异常掩盖根因。

        副作用:
            将 delegation 置 failed 并发出对应 runtime event；写入失败日志。
        """

        if not delegation_id:
            return
        try:
            delegation_service.mark_failed(
                delegation_id,
                error,
                runtime_event_loop=runtime_event_loop,
            )
        except Exception:
            log.exception(
                "delegation_fail_finalize_error",
                extra={
                    "msg": "委派终态化（mark_failed）失败，并发额度可能无法释放",
                    "data": {
                        "delegation_id": delegation_id,
                        "error": error,
                    },
                },
            )

    def _finalize_result(
        self,
        delegation_id: int,
        result: DelegationResult,
        runtime_event_loop: asyncio.AbstractEventLoop | None,
        delegation_service: DelegationService,
        child_task_id: int | None = None,
        title: str = "",
        child_agent_id: str = "",
    ) -> ToolObservation:
        """根据 child 终态更新 delegation 并返回父工具 observation。

        参数:
            delegation_id: 当前 delegation 标识。
            result: child runner 返回的终态结果。
            runtime_event_loop: 父运行时事件循环；用于线程安全发布 delegation 事件。
            delegation_service: 本次执行已解析出的委派生命周期 service。
            child_task_id: 可选的 child task 标识；传入时一并落库便于前端跳转。

        返回:
            success 或 error ToolObservation。

        异常:
            KeyError: 如果 delegation 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果终态持久化失败。

        副作用:
            更新 delegation 终态并发出对应 runtime event。
        """

        if result.status == "completed":
            summary = result.summary or "child run completed"
            delegation_service.mark_completed(
                delegation_id,
                summary,
                child_task_id=child_task_id,
                runtime_event_loop=runtime_event_loop,
            )
            return tool_success(
                "delegate_task",
                "delegate_task",
                summary,
                display_data=build_delegation_display_data(
                    title=title,
                    child_agent_id=child_agent_id,
                    delegation_id=delegation_id,
                    child_task_id=child_task_id,
                    child_run_id=result.child_run_id,
                    status="completed",
                ),
            )
        if result.status == "cancelled":
            error = result.error or "child run cancelled"
            delegation_service.mark_cancelled(
                delegation_id,
                error,
                child_task_id=child_task_id,
                runtime_event_loop=runtime_event_loop,
            )
            return tool_cancelled(
                "delegate_task",
                permission="delegate_task",
            )
        error = result.error or "child run failed"
        delegation_service.mark_failed(
            delegation_id,
            error,
            child_task_id=child_task_id,
            runtime_event_loop=runtime_event_loop,
        )
        return self._child_error("failed", error)

    def _child_error(self, status: str, error: str) -> ToolObservation:
        """构造 child 失败的 delegate_task 工具错误 observation。

        注意：本方法只服务于 child **失败**分支；child **取消**已由 :func:`tool_cancelled`
        单独处理（``_finalize_result`` 的 cancelled 分支），不再走此路径，以免取消被
        塌缩成 error。

        参数:
            status: child 委派失败终态。
            error: child 失败原因。

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
