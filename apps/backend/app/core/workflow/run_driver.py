"""驱动一次 Conversation Run：对话场景的装配方，也是 Run 失败的唯一异常边界。

Task 的 ``TaskActor`` 决定「什么时候跑」，:class:`RunDriver` 负责「怎么跑一次」：读取 Run、Task
与工作区，合成本次 Run 的 Agent 运行时配置，把 Run / Task 级事实（Task 运行空间，以及 Transport
projector 的事件回调）装进 ``RuntimeContext``，再交给 core 的 ``ReactLikeWorkflow``。进程级单例
（Run 状态写入、外部消息队列、工具系统）由 workflow 节点直接取用，不经这里转手；事件回调必须
由这里注入，因为 core 不依赖 Assistant Transport。Run 生命周期与持久进度由领域服务维护，模型
上下文归 Task。
"""

from __future__ import annotations

import asyncio

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.context.tool_call_closure import ContextProtocolError
from app.core.events import ToolCallStatusChangedEvent
from app.core.llm_provider.model_failure import (
    classify_model_failure,
    extract_model_response_message,
)
from app.core.tool.background.backgraound_registry import background_registry
from app.core.tool.background.cancel_placeholders import INTERRUPTED_HINT, cancel_placeholders
from app.core.workflow.cancel.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.workflow.execution_state.runtime_context import RuntimeContext
from app.core.workflow.workflow_run_failure import WorkflowRunFailure
from app.models import ConversationRunRecord
from app.models.conversation_run_failure import run_failure_message
from app.models.enums.conversation_run_status import RunTransition
from app.service.depends import (
    get_conversation_event_projector,
    get_conversation_run_ledger,
    get_conversation_task_context_service,
)


class RunDriver:
    """单个 Run 的驱动方：一次构造对应一次驱动，实例不跨 Run 复用。

    运行期依赖在此装配，驱动步骤各占一个方法，失败落定复用同一份实例状态。本次执行的其余事实
    （Task、工作区、Agent 配置、mailbox 与事件回调）由 ``RuntimeContext.for_run`` 只凭 Run 装配。
    """

    def __init__(self) -> None:
        from app.core.workflow.workflow import ReactLikeWorkflow

        self._ledger = get_conversation_run_ledger()
        self._workflow = ReactLikeWorkflow()

    async def drive(
        self, run: ConversationRunRecord, preceding_run: ConversationRunRecord | None
    ) -> None:
        """驱动一次 Run：装配运行上下文 → 驱动 workflow，异常在此落定。

        ``preceding_run`` 是这次驱动之前 Task 的当前 Run（见 ``RuntimeContext.preceding_run``），由
        调度方给出，只用于推导驱动开始时的通知。

        正常终态由 workflow 节点落定。任何 ``Exception``（含装配期）在这里分类并落定 failed，再把
        仍未收束的工具调用批量收束；Run 已落终态时条件迁移落空，不覆盖既有终态。落定自身失败
        时先记录再上浮。

        ``CancelledError``（进程关闭）照常上浮：关闭不调度后续 Run，遗留 running Run 由下次
        启动收敛。

        副作用:
            结束前回收本 Run 遗留的工具级取消信号（Task 级信号归调度方的释放边界）。
        """

        try:
            rc = RuntimeContext.for_run(run, preceding_run)
            await self._workflow.run(rc)

        except Exception as exc:
            # 即使失败本身落库出错，未提交的工作也不能继续等待启动。
            withdrawn = background_registry.withdraw_uncommitted(run.task_id, run.id)
            if withdrawn:
                await asyncio.to_thread(cancel_placeholders, run.task_id, withdrawn, "启动失败")
            if self._settle_failure(run, exc):
                await asyncio.to_thread(self._close_tool_calls, run)
        finally:
            # 兜底回收「点名了已结束的工具调用」这类不会再被消费的工具级信号。
            tool_call_cancellation_registry.clear_task(run.task_id)

    def _settle_failure(self, run: ConversationRunRecord, exc: Exception) -> bool:
        """把异常归类成失败 code 并落定 failed，返回本次是否真的落定了。

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
        return failed is not None

    def _close_tool_calls(self, run: ConversationRunRecord) -> None:
        """失败打断了工具执行：把没有结果的调用写成事实，再投影。

        不能只改投影：等用户决定的占位已经是调用的结果，决定在 Run 失败后仍可作出，它在库里
        还在等，卡片就必须还在等。只有真正断掉的才结束：没有结果行的调用，以及决定已认领、
        执行被打断的 HITL 占位（Task 同时只有一个执行者，所以 ``running`` 的占位都属于这次）。
        配对依据损坏时不补结果行，保留事实，这些调用的快照要到下次重建才收敛；已认领的决定不靠
        配对，照常结束。同步写库，调用方放在工作线程。
        """
        context = get_conversation_task_context_service()
        repaired: list[str] = []
        try:
            repaired = context.close_unclosed_tool_calls_for_run(run.task_id, run.id)
        except ContextProtocolError as error:
            log.error(
                "run_failure_context_protocol_invalid",
                extra={
                    "msg": "上下文配对依据不完整，未闭合调用留待重建收敛",
                    "data": {"task_id": run.task_id, "run_id": run.id, **error.log_data()},
                },
            )
        projector = get_conversation_event_projector()
        for tool_call_id in repaired:
            projector.process(
                ToolCallStatusChangedEvent(
                    task_id=run.task_id,
                    run_id=run.id,
                    tool_call_id=tool_call_id,
                    status="cancelled",
                )
            )
        cancel_placeholders(run.task_id, context.claimed_decisions(run.task_id), INTERRUPTED_HINT)


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
