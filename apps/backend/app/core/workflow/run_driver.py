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
from app.core.context.tool_call_closure import ContextProtocolError
from app.core.events import ToolCallsSettledEvent
from app.core.hook import HookContext, HookEvent, HookInterceptor
from app.core.llm_provider.model_failure import (
    classify_model_failure,
    extract_model_response_message,
)
from app.core.workflow.cancel.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.workflow.execution_state.runtime_context import RuntimeContext
from app.core.workflow.workflow_run_failure import WorkflowRunFailure
from app.models import ConversationRunRecord, ConversationRunStatus
from app.models.conversation_run_failure import run_failure_message
from app.models.enums.conversation_run_status import RunTransition
from app.service.depends import (
    get_conversation_event_projector,
    get_conversation_run_ledger,
    get_task_service,
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

    运行期依赖在此装配，驱动步骤各占一个方法，失败落定复用同一份实例状态。本次执行的其余事实
    （Task、工作区、Agent 配置、mailbox 与事件回调）由 ``RuntimeContext.for_run`` 只凭 Run 装配。
    """

    def __init__(self) -> None:
        from app.core.workflow.workflow import ReactLikeWorkflow
        self._ledger = get_conversation_run_ledger()
        self._task_service = get_task_service()
        self._workflow = ReactLikeWorkflow()

    async def drive(self, run: ConversationRunRecord) -> None:
        """驱动一次 Run：装配运行上下文 → 驱动 workflow，异常在此落定。

        正常终态由 workflow 节点落定。任何 ``Exception``（含装配期）在这里分类并落定 failed，再把
        仍未收束的工具调用批量收束；Run 已落终态时条件迁移落空，不覆盖既有终态。落定自身失败
        时先记录再上浮。

        ``CancelledError``（进程关闭）照常上浮：关闭不调度后续 Run，遗留 running Run 由下次
        启动收敛。

        副作用:
            触发 USER_PROMPT_SUBMIT 与 STOP（正常收束 / 取消 / 挂起）Hook；结束前回收本 Run 遗留的
            工具级取消信号（Task 级信号归调度方的释放边界）。
        """

        try:
            task = self._task_service.get_task(run.task_id)
            # 无内置实现；Hook 判定 deny 也不阻断已开始的 Run。
            await HookInterceptor.async_safe_fire(
                HookContext.from_locatable(event=HookEvent.USER_PROMPT_SUBMIT, turn=run, locatable=None)
            )


            rc = RuntimeContext.for_run(run)
            await self._workflow.run(rc)
            if await self._has_settled(run):
                await HookInterceptor.async_safe_fire(
                    HookContext.from_locatable(event=HookEvent.STOP, locatable=task, turn=run)
                )

        except Exception as exc:
            self._settle_failure(run, exc)
        finally:
            # 兜底回收「点名了已结束的工具调用」这类不会再被消费的工具级信号。
            tool_call_cancellation_registry.clear_task(run.task_id)

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
