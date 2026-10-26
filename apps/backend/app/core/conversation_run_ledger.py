"""Conversation Run 的事实台账：Run 行的唯一读写入口。

单一职责：Run 的创建、读取、生命周期迁移、执行进度与已完成结果改写都只经本类，其余层不得直接
对 ``conversation_runs`` 做写操作。状态迁移以 ``RunTransition`` 迁移表加一次原子条件更新作为
幂等闸门，只有条件更新真正命中的迁移才发布 ``RunStatusChangedEvent``。

事务与事件：未传入外部 ``session`` 时每次写入自成一次事务并在提交后发布事件；传入外部
``session`` 时加入调用方事务且**不发布事件**，提交后的投影由调用方负责（编辑重跑在提交后重建
snapshot，重启收敛时尚无订阅者）。创建 Run 在提交 Task 当前 Run 指针后投影
``RunInitializedEvent``。
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.orm import Session

from app.config.logging.logger import log
from app.core.events import (
    RunInitializedEvent,
    RunResultUpdatedEvent,
    RunStatusChangedEvent,
)
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.models import ConversationRunRecord, ConversationRunStatus
from app.models.conversation_run_failure import run_terminal_error
from app.models.conversation_run_usage import ConversationRunUsage
from app.models.enums.conversation_run_status import RunTransition
from app.models.react_execution_state import ReactExecutionState
from app.core.workflow.inbox.task_message import RunLaunch
from app.service import depends as service_depends
from app.service.depends import get_conversation_event_projector, get_model_config_service
from app.storage.store_engines import main_session_factory


class ConversationRunLedger:
    """Run 事实的唯一读写方：创建、读取、生命周期迁移、执行进度与结果改写。"""

    def __init__(self) -> None:
        """绑定 Task / Run CRUD 与会话工厂。

        异常:
            RuntimeError: 如果 storage 尚未初始化。
        """

        self._task = service_depends.get_task_crud()
        self._run = service_depends.get_conversation_run_crud()
        self._session_factory = main_session_factory()

    def create_run(self, task_id: int, launch: RunLaunch) -> ConversationRunRecord:
        """新建 Run 的唯一路径：同一事务内按发起配置建 running Run → 写 Task 当前 Run 指针。

        配置由调度方按 ``dispatch_policy.next_action`` 从队列选定，这里不读队列；输入仍在外部
        队列，由新执行的初始投递交给 ``prepare_input`` 并入。只由 Task 的调度 actor（``TaskActor``）
        调用并随即交给执行器，因此直接以 running 创建。
        """

        config = get_model_config_service().get_config(launch.model_config_id)
        with self._session_factory.begin() as session:
            run = self._run.create(
                task_id,
                ConversationRunStatus.RUNNING.value,
                agent_id=launch.agent_id,
                model_config_id=launch.model_config_id,
                context_window_total=config.context_window_k * 1000,
                options=launch.options,
                session=session,
            )
            self._task.set_current_run_id(task_id, run.id, session=session)
        get_conversation_event_projector().process(
            RunInitializedEvent(
                task_id=task_id,
                run_id=run.id,
                context_window_total=run.context_window_total,
                model_config_id=run.model_config_id,
                options=run.options,
            ),
        )
        return run

    def get_run(self, run_id: int) -> ConversationRunRecord:
        """按标识读取单个 Run 记录。

        参数:
            run_id: Conversation Run 标识。

        返回:
            对应的 ``ConversationRunRecord``。

        异常:
            KeyError: run 不存在。

        副作用:
            无。
        """

        return self._run.get(run_id)

    def transition(
            self,
            run_id: int,
            transition: RunTransition,
            *,
            end_reason: str | None = None,
            final_output: str | None = None,
            usage_stats: ConversationRunUsageStats | None = None,
            error_message: str | None = None,
            session: Session | None = None,
    ) -> ConversationRunRecord | None:
        """按迁移表执行一次条件迁移；当前状态不在来源集合内时返回 ``None``，不写库、不发事件。

        参数:
            end_reason / final_output / usage_stats: 随迁移写入的结果字段，``None`` 表示不改。
            error_message: 失败或取消时覆盖受控错误文案（如模型 HTTP 响应体的 ``message``）。
            session: 外部事务；传入时加入该事务且不发布事件（见模块 docstring）。
        """

        error = run_terminal_error(transition.target, end_reason, message=error_message)
        record = self._run.update_status_if_in(
            run_id=run_id,
            target_status=transition.target.value,
            allowed_statuses=tuple(transition.sources),
            end_reason=end_reason,
            final_output=final_output,
            usage=self._persisted_usage(usage_stats),
            error=error,
            session=session,
            clear_terminal_fields=transition.resets_result,
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

        get_conversation_event_projector().process(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=transition.target,
                end_reason=end_reason,
                usage_stats=usage_stats,
                error=error,
            )
        )
        return record

    def save_execution(self, run_id: int, data: ReactExecutionState) -> None:
        """整体保存执行状态（``ReactExecutionState``）；不迁移生命周期状态。"""

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
            usage=self._persisted_usage(usage_stats),
        )
        if record is not None:
            get_conversation_event_projector().process(
                RunResultUpdatedEvent(
                    task_id=record.task_id,
                    run_id=run_id,
                    end_reason=record.end_reason,
                    usage_stats=usage_stats,
                )
            )
        return record

    def _persisted_usage(
            self, usage_stats: ConversationRunUsageStats | None
    ) -> ConversationRunUsage | None:
        """运行时 token 累加器 → Run 行六键用量；``None`` 表示不写用量列。"""

        if usage_stats is None:
            return None
        return cast("ConversationRunUsage | None", usage_stats.to_dict())
