"""Conversation Run 生命周期状态机。

单一职责：作为 Run 状态迁移的唯一入口——以「状态白名单 + 原子条件更新」作为幂等闸门，
仅在条件更新真正命中后才发布 ``RunStatusChangedEvent``，随后回读并返回最新记录；并提供
「task 是否已有 active run」查询。Run 记录读取（``get_run`` / ``list_runs_for_task``）
已迁至 ``ConversationRunService``。

职责边界：
- 负责：``pending``、``running``、``waiting_for_input`` 与终态之间的条件迁移及其事件发布、
  active run 判定查询，以及已 ``completed`` Run 的结果字段改写（不迁移状态，发布结果事件）。
- 不负责：Run 记录读取（见 ``ConversationRunService``）；命令输入解析与附件处理、Run 创建与
  编辑的用例编排（见 ``ConversationRunService``）；崩溃恢复中「Run 终态 + 未闭合工具调用」
  的多事实收口同样由 ``ConversationRunService`` 编排。
- 不拥有跨表事务：各迁移方法在 CRUD 的条件更新内自成一次写入；仅 ``has_active_run``
  接受调用方传入的外部 ``session``，此时事务提交与事件发布由调用方负责。

持久化事实归 ``ConversationRunCrud``；本模块表达合法状态迁移及已完成结果更新的事件。
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.orm import Session

from app.assistant_transport.event import RunResultUpdatedEvent, RunStatusChangedEvent
from app.assistant_transport.event.dispatch import dispatch_conversation_event
from app.config.constant import Constant
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.models import (
    ConversationRunError,
    ConversationRunRecord,
    ConversationRunStatus,
)
from app.models.conversation_run_failure import (
    run_failure_message,
)
from app.models.conversation_run_progress import ConversationRunProgress
from app.models.conversation_run_usage import ConversationRunUsage
from app.models.user_decision import UserDecision
from app.service import depends as service_depends
from app.storage.store_engines import main_session_factory

_RUN_STATUS_TRANSITIONS: dict[ConversationRunStatus, tuple[ConversationRunStatus, ...]] = {
    ConversationRunStatus.PENDING: (
        ConversationRunStatus.RUNNING,
        ConversationRunStatus.FAILED,
        ConversationRunStatus.CANCELLED,
    ),
    ConversationRunStatus.RUNNING: (
        ConversationRunStatus.WAITING_FOR_INPUT,
        ConversationRunStatus.COMPLETED,
        ConversationRunStatus.FAILED,
        ConversationRunStatus.CANCELLED,
    ),
    ConversationRunStatus.WAITING_FOR_INPUT: (
        ConversationRunStatus.RUNNING,
        ConversationRunStatus.CANCELLED,
    ),
    ConversationRunStatus.CANCELLED: (ConversationRunStatus.RUNNING,),
    ConversationRunStatus.COMPLETED: (),
    ConversationRunStatus.FAILED: (ConversationRunStatus.RUNNING,),
}


class ConversationRunStateService:
    """Conversation Run 状态机与状态迁移的唯一入口；Run 记录读取见 ``ConversationRunService``。"""

    def __init__(self) -> None:
        """初始化状态 service：装配 run CRUD 与会话工厂。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 storage 尚未初始化。

        副作用:
            从 service 依赖入口取得 CRUD 单例并保存引用；不读写任何 run 状态。
        """

        self._run = service_depends.get_conversation_run_crud()
        self._session_factory = main_session_factory()

    @staticmethod
    def _allowed_source_statuses(
        target_status: ConversationRunStatus,
        candidates: tuple[ConversationRunStatus, ...] | None = None,
    ) -> tuple[str, ...]:
        """从唯一状态图按候选范围派生允许迁入目标状态的来源状态值。"""

        return tuple(
            source.value
            for source, targets in _RUN_STATUS_TRANSITIONS.items()
            if target_status in targets and (candidates is None or source in candidates)
        )

    @staticmethod
    def usage_payload(
        usage_stats: ConversationRunUsageStats | None,
    ) -> ConversationRunUsage | None:
        """把运行时 token 累加器转成 Run 行可持久化的六键字典。

        参数:
            usage_stats: 运行期累加器；``None`` 表示本次不写用量列（例如崩溃恢复）。

        返回:
            ``ConversationRunUsage`` 字典或 ``None``。

        异常:
            无；累加器自身保证返回可 JSON 序列化的整数/None 字段。

        副作用:
            无。
        """

        if usage_stats is None:
            return None
        return cast("ConversationRunUsage | None", usage_stats.to_dict())

    @staticmethod
    def terminal_error(
        status: ConversationRunStatus,
        end_reason: str | None,
        message: str | None = None,
    ) -> ConversationRunError | None:
        """把终态状态映射为持久化错误契约。

        参数:
            status: 目标终态；``completed`` 表示成功终止，不写错误契约。
            end_reason: 领域侧终态原因；仅当它是合法标识符（``isidentifier()``）时才作为
                ``code`` 落库，避免把自由文本写进受控字段。
            message: 可选的错误消息覆盖值；模型失败时用于透传 HTTP 响应体的 ``message`` 字段，
                为空或全空白时仍使用受控文案目录。

        返回:
            非 ``completed`` 终态返回受控的 ``ConversationRunError``；``completed`` 返回 ``None``。
            ``message`` 优先使用非空覆盖值，否则由失败 code 目录生成受控文案；未登记的 code
            回退通用失败文案，保证 UI 始终拿得到可展示文本。

        异常:
            无。

        副作用:
            无。
        """

        if status is ConversationRunStatus.COMPLETED:
            return None
        code = end_reason if isinstance(end_reason, str) and end_reason.isidentifier() else None
        if code is None:
            code = (
                Constant.Run.RUN_FAILURE_CODE_CANCELLED
                if status is ConversationRunStatus.CANCELLED
                else Constant.Run.RUN_FAILURE_CODE_UNKNOWN
            )
        resolved_message = message.strip() if isinstance(message, str) else ""
        return ConversationRunError(
            code=code,
            message=resolved_message or run_failure_message(code),
        )

    def has_active_run(self, task_id: int, session: Session | None = None) -> bool:
        """检查该 task 是否已存在 ``pending``、``running`` 或等待输入的 Run。

        这是「一个 task 同一时刻只允许一个 active run」这条不变量的读侧表达。注意它
        **不是** Task 操作闸门的重复：闸门只把并发操作串行化，并不阻止两条不同
        ``command_id`` 的请求依次各建一个 active run。

        参数:
            task_id: 任务标识。
            session: 可选，调用方已开启的事务会话；为 None 时本方法自建只读会话。

        返回:
            该 task 存在 ``pending``、``running`` 或
            ``waiting_for_input`` Run 时返回 True，否则 False。

        异常:
            无；底层会话或查询失败按 SQLAlchemy 语义向上传播。

        副作用:
            无（仅只读查询）。
        """

        statuses = (
            ConversationRunStatus.PENDING.value,
            ConversationRunStatus.RUNNING.value,
            ConversationRunStatus.WAITING_FOR_INPUT.value,
        )
        if session is not None:
            return self._run.has_run_in_status(task_id, statuses, session)
        with self._session_factory() as owned_session:
            return self._run.has_run_in_status(task_id, statuses, owned_session)

    def reset_for_edit(self, run_id: int, session: Session) -> ConversationRunRecord:
        """与消息截断同事务重置 Run；提交后的快照重建统一发布新状态。"""
        previous = self._run.get_in_session(session, run_id)
        progress = ConversationRunProgress(step_count=previous.execution_data.step_count)
        record = self._run.update_status_if_in(
            run_id,
            "running",
            ("completed", "cancelled", "failed"),
            session=session,
            clear_terminal_fields=True,
            execution_data=progress,
        )
        if record is None:
            raise ValueError("只有已停止的 Run 可以编辑重跑")
        return record

    def resume_cancelled_run(self, run_id: int) -> ConversationRunRecord | None:
        """显式恢复 cancelled/failed Run，清空终态字段；执行器从模型边界继续。"""

        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.RUNNING.value,
            self._allowed_source_statuses(
                ConversationRunStatus.RUNNING,
                (ConversationRunStatus.CANCELLED, ConversationRunStatus.FAILED),
            ),
            clear_terminal_fields=True,
        )
        if record is None:
            return None
        dispatch_conversation_event(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.RUNNING,
            ),
        )
        return record

    def mark_waiting_for_input_if_running(
        self,
        run_id: int,
        usage_stats: ConversationRunUsageStats | None = None,
        *,
        execution_data: ConversationRunProgress,
    ) -> ConversationRunRecord | None:
        """把等待数据与 running→waiting_for_input 一起提交；成功后才发布状态。"""

        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.WAITING_FOR_INPUT.value,
            self._allowed_source_statuses(ConversationRunStatus.WAITING_FOR_INPUT),
            execution_data=execution_data,
            end_reason=None,
            error=None,
            usage=self.usage_payload(usage_stats),
        )
        if record is None:
            return None
        dispatch_conversation_event(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.WAITING_FOR_INPUT,
                usage_stats=usage_stats,
            ),
        )
        return record

    def resume_waiting_run(
        self, run_id: int, *, user_decision: UserDecision
    ) -> ConversationRunRecord | None:
        """把决定与 waiting_for_input→running 一起提交，后续执行只消费该决定。"""

        progress = self._run.get(run_id).execution_data
        continuation = progress.continuation
        if continuation is None:
            raise ValueError("等待恢复缺少待决调用")
        progress = progress.model_copy(
            update={"continuation": continuation.model_copy(update={"decision": user_decision})}
        )
        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.RUNNING.value,
            self._allowed_source_statuses(
                ConversationRunStatus.RUNNING,
                (ConversationRunStatus.WAITING_FOR_INPUT,),
            ),
            clear_terminal_fields=True,
            execution_data=progress,
        )
        if record is None:
            return None
        dispatch_conversation_event(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.RUNNING,
            ),
        )
        return record

    def claim_pending_run(self, run_id: int) -> ConversationRunRecord | None:
        """认领一个待执行的 Conversation Run（``pending`` → ``running``）。

        参数:
            run_id: 待认领的运行标识。

        返回:
            认领成功时返回更新后的 ``ConversationRunRecord``；run 已不是 ``pending``
            （终态、或已被其它执行器认领为 ``running``）时返回 None。

        异常:
            KeyError: run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 写入失败。

        副作用:
            pending run 成功迁移时发布一次 RUNNING 状态事件；未命中时不写库、不发事件。
        """

        row = self._run.update_status_if_in(
            run_id=run_id,
            target_status=ConversationRunStatus.RUNNING.value,
            allowed_statuses=self._allowed_source_statuses(
                ConversationRunStatus.RUNNING,
                (ConversationRunStatus.PENDING,),
            ),
        )
        if row is not None:
            dispatch_conversation_event(
                RunStatusChangedEvent(
                    task_id=row.task_id,
                    run_id=run_id,
                    status=ConversationRunStatus.RUNNING,
                ),
            )
            return self._run.get(run_id)

        return None

    def complete_run_if_running(
        self,
        run_id: int,
        final_output: str | None = None,
        usage_stats: ConversationRunUsageStats | None = None,
        *,
        end_reason: str | None = None,
    ) -> ConversationRunRecord | None:
        """原子完成仍处于 running 的 Conversation Run。

        业务语义：仅 ``running`` 可进入 ``completed`` 终态并落库回复文本；约束收敛在
        本方法（service 层），CRUD 层只做通用的「状态白名单 + 原子更新」。Agent 对该
        轮次的最终回答文本通过 ``final_output`` 一并写入，供快速检索与审计。

        参数:
            run_id: 待完成的 Conversation Run 标识。
            final_output: 可选，Agent 对该轮次的最终回答文本；为 None 时不修改该列。
            usage_stats: 可选，运行用量统计，终态时按六键契约落库并发布事件。
            end_reason: 可选，模型原始完成原因，随状态与最终回答原子落库。

        返回:
            成功完成时返回更新后的 ConversationRunRecord；run 已不是 running 时返回 None。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时更新 run 状态为 completed（并可选写入结果、原因与用量），随后
            发布 COMPLETED 状态事件。
        """

        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.COMPLETED.value,
            self._allowed_source_statuses(ConversationRunStatus.COMPLETED),
            end_reason,
            final_output=final_output,
            usage=self.usage_payload(usage_stats),
            error=None,
        )
        if record is None:
            return None
        dispatch_conversation_event(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.COMPLETED,
                end_reason=end_reason,
                usage_stats=usage_stats,
            ),
        )
        return record

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
            usage=self.usage_payload(usage_stats),
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

    def fail_run_if_running(
        self,
        run_id: int,
        end_reason: str | None = None,
        final_output: str | None = None,
        usage_stats: ConversationRunUsageStats | None = None,
        error_message: str | None = None,
    ) -> ConversationRunRecord | None:
        """原子失败结束仍处于可失败状态的 Run，并发布错误事件。

        参数:
            run_id: 待失败落定的 Conversation Run 标识。
            end_reason: 可选失败原因。
            final_output: 可选，随终态一并写入的失败说明文本，供委派场景主 Agent 感知。
            usage_stats: 可选，运行用量统计，终态时按六键契约落库并发布事件。
            error_message: 可选的 UI 错误消息覆盖值；workflow 可传入模型 HTTP 响应体的
                ``message`` 字段。为空时沿用受控错误文案。

        返回:
            成功失败落定时返回更新后的 ConversationRunRecord；run 已不是 running 时返回 None。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时更新 run 状态为 failed（并可选写入 end_reason 与 final_output），
            随后发布 FAILED 状态事件；事件携带同一份错误契约，使前端无需额外查询即可展示失败
            原因。提供 ``error_message`` 时它会随 Run 持久化并透传到 Transport。
        """

        error = self.terminal_error(
            ConversationRunStatus.FAILED,
            end_reason,
            message=error_message,
        )
        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.FAILED.value,
            self._allowed_source_statuses(ConversationRunStatus.FAILED),
            end_reason,
            final_output=final_output,
            usage=self.usage_payload(usage_stats),
            error=error,
        )
        if record is None:
            return None
        dispatch_conversation_event(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.FAILED,
                end_reason=end_reason,
                usage_stats=usage_stats,
                error=error,
            ),
        )
        return record

    def cancel_run_if_running(
        self,
        run_id: int,
        end_reason: str = "user_cancelled",
        final_output: str | None = None,
        usage_stats: ConversationRunUsageStats | None = None,
    ) -> ConversationRunRecord | None:
        """将 active Run 标记 cancelled，并发布其 Transport 展示状态。

        终态同时写入 ``final_output``，使复用同一工作流的子 Agent 即便被取消，主 Agent
        也能从委派结果中感知其已产出（或被中断）的内容，而非仅看到一个空终态。

        参数:
            run_id: 待取消的 Conversation Run 标识。
            end_reason: 稳定的取消原因。
            final_output: 可选，随终态一并写入的取消说明/部分输出文本，供委派场景主 Agent 感知。
            usage_stats: 可选，运行用量统计，终态时按六键契约落库并发布事件。

        返回:
            成功落定时返回更新后的 ConversationRunRecord；run 已是终态时返回 None。

        异常:
            KeyError: run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 写入失败。

        副作用:
            条件满足时将 ``pending`` / ``running`` 更新为 ``cancelled``（并可选写入
            end_reason、final_output 与用量），随后发布 CANCELLED 状态事件；事件同时携带
            同一份受控错误契约，使前端无需额外查询即可展示终止原因。
        """

        error = self.terminal_error(ConversationRunStatus.CANCELLED, end_reason)
        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.CANCELLED.value,
            self._allowed_source_statuses(
                ConversationRunStatus.CANCELLED,
                (ConversationRunStatus.PENDING, ConversationRunStatus.RUNNING),
            ),
            end_reason,
            final_output=final_output,
            usage=self.usage_payload(usage_stats),
            error=error,
        )
        if record is None:
            return None
        dispatch_conversation_event(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.CANCELLED,
                end_reason=end_reason,
                usage_stats=usage_stats,
                error=error,
            ),
        )
        return record
