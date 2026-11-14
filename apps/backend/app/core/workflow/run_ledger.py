"""Conversation Run 的事实台账：Run 行的唯一读写入口。

单一职责：Run 的创建、读取、生命周期迁移、执行进度与已完成结果改写都只经本类，其余层不得直接
对 ``runs`` 做写操作。状态迁移以 ``RunTransition`` 迁移表加一次原子条件更新作为
幂等闸门，只有条件更新真正命中的迁移才发布 ``RunStatusChangedEvent``。

事务与事件：未传入外部 ``db`` 时每次写入自成一次事务并在提交后发布事件；传入外部
``db`` 时加入调用方事务且**不发布事件**，提交后的投影由调用方负责（撤回在提交后重建
snapshot，重启收敛时尚无订阅者）。创建 Run 在提交 Session 当前 Run 指针后投影
``RunInitializedEvent``。
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.orm import Session as DbSession

from app.config.logging.logger import log
from app.core.events import RunInitializedEvent, RunResultUpdatedEvent, RunStatusChangedEvent
from app.core.events.event_sink import event_sink
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.execution_state.run_usage_stats import RunUsageStats
from app.core.workflow.message_queue.session_message import RunLaunch
from app.models import RunRecord, RunStatus
from app.models.enums.run_status import RunTransition
from app.models.run_failure import run_terminal_error
from app.models.run_usage import RunUsage
from app.service import depends as service_depends
from app.service.depends import get_model_config_service
from app.storage.store_engines import main_db_factory


class RunLedger:
    """Run 事实的唯一读写方：创建、读取、生命周期迁移、执行进度与结果改写。"""

    def __init__(self) -> None:
        """绑定 Session / Run CRUD 与 db 工厂。

        异常:
            RuntimeError: 如果 storage 尚未初始化。
        """

        self._session = service_depends.get_session_crud()
        self._run = service_depends.get_run_crud()
        self._db_factory = main_db_factory()

    def create_run(self, session_id: int, launch: RunLaunch) -> RunRecord:
        """新建 Run 的唯一路径：同一事务内按发起配置建 running Run → 写 Session 当前 Run 指针。

        配置由调度方按 ``dispatch_policy.next_action`` 从队列选定，这里不读队列；输入仍在外部
        队列，由新执行的初始投递交给 ``prepare_input`` 并入。只由 Session 的调度 actor（``SessionActor``）
        调用并随即交给执行器，因此直接以 running 创建。
        """

        config = get_model_config_service().get_config(launch.model_config_id)
        with self._db_factory.begin() as db:
            run = self._run.create(
                session_id,
                RunStatus.RUNNING.value,
                agent_id=launch.agent_id,
                model_config_id=launch.model_config_id,
                context_window_total=config.model.context_window_k * 1000,
                reasoning_effort=launch.reasoning_effort,
                db=db,
            )
            self._session.set_current_run_id(session_id, run.id, db=db)
        event_sink()(
            RunInitializedEvent(
                session_id=session_id,
                run_id=run.id,
                created_at=run.created_at,
                context_window_total=run.context_window_total,
                model_config_id=run.model_config_id,
                reasoning_effort=run.reasoning_effort,
            ),
        )
        return run

    def get_run(self, run_id: int) -> RunRecord:
        """按标识读取单个 Run 记录。

        参数:
            run_id: Conversation Run 标识。

        返回:
            对应的 ``RunRecord``。

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
        usage_stats: RunUsageStats | None = None,
        error_message: str | None = None,
        response_sequence: int | None = None,
        db: DbSession | None = None,
    ) -> RunRecord | None:
        """按迁移表执行一次条件迁移；当前状态不在来源集合内时返回 ``None``，不写库、不发事件。

        参数:
            end_reason / final_output / usage_stats: 随迁移写入的结果字段，``None`` 表示不改。
            error_message: 失败或取消时覆盖受控错误文案（如模型 HTTP 响应体的 ``message``）。
            db: 外部事务；传入时加入该事务且不发布事件（见模块 docstring）。
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
            db=db,
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
        if record is None or db is not None:
            return record

        event_sink()(
            RunStatusChangedEvent(
                session_id=record.session_id,
                run_id=run_id,
                status=transition.target,
                end_reason=end_reason,
                usage_stats=usage_stats,
                error=error,
                changed_at=record.updated_at,
                response_sequence=response_sequence,
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
        usage_stats: RunUsageStats | None = None,
    ) -> RunRecord | None:
        """结构化支线只改写已完成 Run 的结果与用量，不重新发布生命周期事件。"""

        record = self._run.update_completed_result(
            run_id,
            final_output=final_output,
            end_reason=end_reason,
            usage=self._persisted_usage(usage_stats),
        )
        if record is not None:
            event_sink()(
                RunResultUpdatedEvent(
                    session_id=record.session_id,
                    run_id=run_id,
                    end_reason=record.end_reason,
                    usage_stats=usage_stats,
                )
            )
        return record

    def _persisted_usage(
        self, usage_stats: RunUsageStats | None
    ) -> RunUsage | None:
        """运行时 token 累加器 → Run 行六键用量；``None`` 表示不写用量列。"""

        if usage_stats is None:
            return None
        return cast("RunUsage | None", usage_stats.to_dict())
