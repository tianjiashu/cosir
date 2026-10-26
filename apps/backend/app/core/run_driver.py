"""驱动一次 Conversation Run：对话场景的装配方，也是 Run 失败的唯一异常边界。

Task 的 ``TaskActor`` 决定「什么时候跑」，:class:`RunDriver` 负责「怎么跑一次」：读取 Run、Task 与工作区，
合成本次 Run 的 Agent 运行时配置，把 Run / Task 级事实（Task 运行空间，以及 Transport projector
的事件回调）装进 ``RuntimeContext``，再交给 core 的 ``ReactLikeWorkflow``。进程级单例（Run 状态
写入、外部消息队列、工具系统）由 workflow 节点直接取用，不经这里转手；事件回调必须由这里注入，
因为 core 不依赖 Assistant Transport。Run 生命周期与持久进度由领域服务维护，模型上下文归 Task。
"""

from __future__ import annotations

import asyncio

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.agents.agent_runtime_config import AgentRuntimeConfig
from app.core.context.tool_call_closure import ContextProtocolError
from app.core.events import ToolCallsSettledEvent
from app.core.hook import HookContext, HookEvent, HookInterceptor
from app.core.llm_provider.model_failure import (
    classify_model_failure,
    extract_model_response_message,
)
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.core.workflow.cancel.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.workflow.execution_state.runtime_context import RuntimeContext
from app.core.workflow.inbox.run_mailbox import RunMailbox
from app.core.workflow.workflow_run_failure import WorkflowRunFailure
from app.models import ConversationRunRecord, ConversationRunStatus
from app.models.conversation_run_failure import run_failure_message
from app.models.enums.conversation_run_status import RunTransition
from app.core.workflow.inbox.task_message import TaskMessage
from app.service.depends import (
    get_conversation_event_projector,
    get_conversation_run_ledger,
    get_task_service,
    get_workspace_service,
)

_STOP_HOOK_STATUSES = frozenset(
    {
        ConversationRunStatus.COMPLETED.value,
        ConversationRunStatus.CANCELLED.value,
        ConversationRunStatus.SUSPENDED.value,
    }
)


class RunDriver:
    """单个 Run 的驱动方：一次构造对应一次驱动，实例不跨 Run 复用。

    构造参数是调度方手里的事实：``run_id``、随本次执行创建的 ``mailbox``、调度方的首次投递
    ``initial_delivery`` 与结束通知 ``on_finished``。运行期依赖在此装配，驱动步骤各占一个方法，
    失败落定与结束通知复用同一份实例状态。
    """

    def __init__(self, mailbox: RunMailbox) -> None:
        """``mailbox`` 由调度方随执行创建并持续投递外部队列消息；``initial_delivery`` 是调度方的首次
        投递（等启动它的那一轮释放 Task 闸门才投递），必须在本驱动的失败边界内执行：它在边界外失败时
        Run 会停在 running 而没有执行者；``on_finished`` 是结束通知，调度方据此释放执行登记。"""
        self._mailbox = mailbox
        self._ledger = get_conversation_run_ledger()
        self._task_service = get_task_service()
        self._workspace_service = get_workspace_service()

    def post(self, message: TaskMessage):
        self._mailbox.post(message)

    async def drive(self, run: ConversationRunRecord) -> None:
        """驱动一次 Run：失败在此落定，结束时通知调度方。

        初始投递 → 装配 Agent 运行时配置与运行上下文 → 驱动 workflow；正常终态由 workflow 节点落定。
        任何 ``Exception``（含初始投递与装配期）在这里分类并落定 failed，再把仍未收束的工具调用批量
        收束；Run 已落终态时条件迁移落空，不覆盖既有终态。正常收束与失败落定后都调用
        ``on_finished``；落定自身失败时先记录再上浮（通知已在 ``finally`` 里发生）。

        ``CancelledError``（进程关闭）照常上浮且不通知：关闭不调度后续 Run，遗留 running Run 由下次
        启动收敛——所以通知不能无条件放 ``finally``。

        副作用:
            触发 USER_PROMPT_SUBMIT 与 STOP（正常收束 / 取消 / 挂起）Hook；结束前回收本 Run 遗留的
            工具级取消信号（Task 级信号归调度方的释放边界）。
        """

        try:
            task = self._task_service.get_task(run.task_id)
            workspace = self._workspace_service.get_workspace(task.workspace_id)
            # 无内置实现；Hook 判定 deny 也不阻断已开始的 Run。
            await HookInterceptor.async_safe_fire(
                HookContext.from_locatable(event=HookEvent.USER_PROMPT_SUBMIT, turn=run, locatable=None)
            )
            from app.core.workflow.workflow import ReactLikeWorkflow

            rc = RuntimeContext.for_run(
                run,
                task,
                workspace,
                AgentRuntimeConfig.for_run(run, task, workspace),
                self._mailbox,
                event_sink=get_conversation_event_projector().process
            )
            await ReactLikeWorkflow().run(rc)
            if await self._has_settled(run):
                await HookInterceptor.async_safe_fire(
                    HookContext.from_locatable(event=HookEvent.STOP, locatable=task, turn=run)
                )

        except Exception as exc:
            self._settle_failure(run, exc)
        finally:
            # 兜底回收「点名了已结束的工具调用」这类不会再被消费的工具级信号。
            tool_call_cancellation_registry.clear_task(run.task_id)
            cancellation_registry.consume(run.task_id)

    async def _has_settled(self, run: ConversationRunRecord) -> bool:
        """workflow 返回后重读状态：只有收束到终态或挂起才触发 STOP Hook。"""

        current = await asyncio.to_thread(self._ledger.get_run, run.id)
        return current.status in _STOP_HOOK_STATUSES

    def _settle_failure(self, run: ConversationRunRecord, exc: Exception) -> None:
        """把异常归类成失败 code 并落定 failed，再收束本 Run 仍未闭合的工具调用。

        落定自身失败（如数据库不可用）时先记录再上浮：后台 task 无人 await，只有这条结构化日志能
        留下「Run 仍停在 running」的 run 级事实，遗留的 running 由下次启动收敛。
        """

        failure_code = _failure_code_for(exc)
        try:
            failed = self._ledger.transition(
                run.id,
                RunTransition.FAIL,
                end_reason=failure_code,
                final_output=run_failure_message(failure_code),
                error_message=extract_model_response_message(exc),
            )
        except Exception:
            log.exception(
                "run_settlement_failed",
                extra={
                    "msg": f"Run 落定 failed 自身失败，失败 code: {failure_code}",
                    "data": {
                        "task_id": run.task_id,
                        "run_id": run.id,
                        "failure_code": failure_code,
                    },
                },
                exc_info=True,
            )
            raise
        log.exception(
            "task_failed",
            extra={
                "msg": f"run 执行失败，失败 code: {failure_code}，错误类型: {type(exc).__name__}",
                "data": {
                    "task_id": run.task_id,
                    "run_id": run.id,
                    "failure_code": failure_code,
                    "error_type": type(exc).__name__,
                },
            },
            exc_info=exc,
        )
        if failed is None:
            return
        # 失败打断了工具执行时，pending / running 的工具调用不会再收到单条迁移。
        get_conversation_event_projector().process(
            ToolCallsSettledEvent(
                task_id=failed.task_id,
                run_id=run.id,
                status="cancelled",
                reason="runtime_failed",
            )
        )


def resolve_agent_config(run: ConversationRunRecord) -> AgentRuntimeConfig:
    """按 Run 的持久化事实合成其 Agent 运行时配置（规则见 ``AgentRuntimeConfig.for_run``）。

    供 Run 之外的执行方复用父 Agent 配置，如用户批准 Agent Team 时为节点回落模型配置。
    """
    task = get_task_service().get_task(run.task_id)
    workspace = get_workspace_service().get_workspace(task.workspace_id)
    return AgentRuntimeConfig.for_run(run, task, workspace)


def _failure_code_for(exc: BaseException) -> str:
    """把异常归类为 Run 失败 code。

    上下文完整性错误用本地 code，不归因于模型服务；``WorkflowRunFailure`` 已自带 code，不二次
    猜测；其余交给模型失败分类器，分类不出时回退中性 code，避免把普通执行错误误报成模型问题。
    """

    if isinstance(exc, ContextProtocolError):
        return Constant.Run.RUN_FAILURE_CODE_CONTEXT_PROTOCOL_INVALID
    if isinstance(exc, WorkflowRunFailure):
        return exc.failure_code
    return classify_model_failure(exc) or Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED
