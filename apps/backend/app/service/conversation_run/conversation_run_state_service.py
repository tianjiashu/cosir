"""Conversation Run 生命周期写入：workflow 与调度入口共用的唯一 Run 状态写入方。

单一职责：按 ``RunTransition`` 迁移表执行 Run 状态迁移——一次原子条件更新作为幂等闸门，
仅在条件更新真正命中后才发布 ``RunStatusChangedEvent``；并承担执行进度保存、已完成 Run 的
结果改写。Run 记录读取见 ``ConversationRunService``。

事务：未传入外部 ``session`` 时每次迁移自成一次写入并在提交后发布事件；传入外部 ``session``
时加入调用方事务且**不发布事件**，提交后的投影由调用方负责（编辑重跑在提交后重建 snapshot，
重启收敛时尚无订阅者）。
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.orm import Session

from app.assistant_transport.event.dispatch import dispatch_conversation_event
from app.config.logging.logger import log
from app.core.events import RunResultUpdatedEvent, RunStatusChangedEvent
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.models import ConversationRunRecord
from app.models.conversation_run_failure import run_terminal_error
from app.models.conversation_run_progress import ConversationRunProgress
from app.models.conversation_run_usage import ConversationRunUsage
from app.models.enums.conversation_run_status import RunTransition
from app.service import depends as service_depends


def _persisted_usage(usage_stats: ConversationRunUsageStats | None) -> ConversationRunUsage | None:
    """运行时 token 累加器 → Run 行六键用量；``None`` 表示不写用量列。"""

    if usage_stats is None:
        return None
    return cast("ConversationRunUsage | None", usage_stats.to_dict())


class ConversationRunStateService:
    """Run 状态迁移、执行进度与已完成结果的唯一写入方。"""

    def __init__(self) -> None:
        self._run = service_depends.get_conversation_run_crud()

    def transition(
        self,
        run_id: int,
        transition: RunTransition,
        *,
        end_reason: str | None = None,
        final_output: str | None = None,
        usage_stats: ConversationRunUsageStats | None = None,
        error_message: str | None = None,
        execution_data: ConversationRunProgress | None = None,
        session: Session | None = None,
    ) -> ConversationRunRecord | None:
        """按迁移表执行一次条件迁移；当前状态不在来源集合内时返回 ``None``，不写库、不发事件。

        参数:
            end_reason / final_output / usage_stats: 随迁移写入的结果字段，``None`` 表示不改。
            error_message: 失败或取消时覆盖受控错误文案（如模型 HTTP 响应体的 ``message``）。
            execution_data: 随迁移原子写入的执行进度（重启收敛时的步骤计数等）。
            session: 外部事务；传入时加入该事务且不发布事件（见模块 docstring）。
        """

        error = run_terminal_error(transition.target, end_reason, message=error_message)
        record = self._run.update_status_if_in(
            run_id=run_id,
            target_status=transition.target.value,
            allowed_statuses=tuple(transition.sources),
            end_reason=end_reason,
            final_output=final_output,
            usage=_persisted_usage(usage_stats),
            error=error,
            session=session,
            clear_terminal_fields=transition.resets_result,
            execution_data=execution_data,
        )
        # 唯一的 Run 状态写入口：每次迁移（含因状态已变而落空的）都留痕，竞态可排查。
        log.info(
            "run_transition",
            extra={
                "msg": f"Run 状态迁移 {transition.name}：{'命中' if record else '落空'}",
                "data": {
                    "run_id": run_id,
                    "transition": transition.name,
                    "applied": record is not None,
                    "end_reason": end_reason,
                },
            },
        )
        if record is None or session is not None:
            return record
        dispatch_conversation_event(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=transition.target,
                end_reason=end_reason,
                usage_stats=usage_stats,
                error=error,
            ),
        )
        return record

    def save_execution(self, run_id: int, data: ConversationRunProgress) -> None:
        """保存恢复所需的执行进度；不迁移生命周期状态。"""

        self._run.save_execution(run_id, data)

    def update_completed_result(
        self,
        run_id: int,
        *,
        final_output: str | None = None,
        end_reason: str | None = None,
        usage_stats: ConversationRunUsageStats | None = None,
    ) -> ConversationRunRecord | None:
        """结构化支线只改写已完成 Run 的结果与用量，不重新发布生命周期事件。"""

        record = self._run.update_completed_result(
            run_id,
            final_output=final_output,
            end_reason=end_reason,
            usage=_persisted_usage(usage_stats),
        )
        if record is not None:
            dispatch_conversation_event(
                RunResultUpdatedEvent(
                    task_id=record.task_id,
                    run_id=run_id,
                    end_reason=record.end_reason,
                    usage_stats=usage_stats,
                )
            )
        return record
