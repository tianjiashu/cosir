"""后端重启恢复用例：重启后的收敛与收尾只在这里按序编排，lifespan 只调用一次入口。"""

from app.config.logging.logger import log
from app.core.context.tool_call_closure import ContextProtocolError
from app.core.tool.background import restart_settlement
from app.models import (
    ConversationRunRecord,
    ConversationRunStatus,
)
from app.models.enums.conversation_run_status import RunTransition
from app.service import depends as service_depends
from app.storage.store_engines import main_session_factory
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class RecoverService:
    """后端启动期的重启恢复：收敛遗留 running Run / TeamRun，拉起待调度 Task，再为延迟结果收尾。

    不动 suspended Run 与 message_queue，不隐式重放旧执行。
    """

    def __init__(self) -> None:
        """绑定恢复所需的 Run CRUD、状态迁移、Task context 与事务工厂。"""

        self._run = service_depends.get_conversation_run_crud()
        self._ledger = service_depends.get_conversation_run_ledger()
        self._context = service_depends.get_conversation_task_context_service()
        self._session_factory = main_session_factory()

    def recover_after_restart(self) -> None:
        """重启恢复的唯一入口。

        调用方须已 ``task_runtime_spaces.bind_loop`` 并打开 ``deferred_registry``：本入口会
        物化 space（拉起 actor）并投递重启结论。

        顺序即契约：

        1. 收敛遗留 Run 与 TeamRun：之后查出的待调度 Task 不含已收敛的 Run，子 Run 回投也依赖
           本步返回的收敛结果。
        2. 为外部队列有待处理消息或当前 Run 挂起的 Task 物化 space（随之拉起 actor），让重启前
           已到达的消息照常处理、挂起的 Run 照常等唤醒。
        3. 把残留的后台占位推到「已中断」，再按 ``parent_task_id`` 回投被收敛的委派子 Run 结论。
        4. 唤醒挂在终端上的 Run，候选与第 2 步拉起 actor 的是同一份；其余 Task 在本进程首次驱动
           时另得失效提醒（``restart_settlement.first_drive_notice``）。
        """

        # coordinator 消费已装配的工具系统，且与 service 层互相引用，惰性导入。
        from app.core.tool.handler.agent_team.coordinator import get_agent_team_coordinator

        recovered_runs = self.recover_orphaned_runs()
        recovered_team_runs = get_agent_team_coordinator().recover_after_restart()
        dispatch_task_ids = service_depends.get_task_service().list_task_ids_to_dispatch()
        for task_id in dispatch_task_ids:
            task_runtime_spaces.get_or_create(task_id)
        interrupted = restart_settlement.interrupt_pending_placeholders()
        settled_child_run_ids = restart_settlement.settle_restarted_child_runs(recovered_runs)
        woken_task_ids = restart_settlement.wake_suspended_terminal_runs(dispatch_task_ids)
        log.info(
            "backend_restart_recovered",
            extra={
                "msg": "后端启动期重启恢复完成",
                "data": {
                    "cancelled_run_ids": [run.id for run in recovered_runs],
                    "cancelled_team_run_count": recovered_team_runs,
                    "dispatch_task_ids": sorted(dispatch_task_ids),
                    "interrupted_placeholder_count": interrupted,
                    "settled_child_run_ids": settled_child_run_ids,
                    "woken_terminal_task_ids": woken_task_ids,
                },
            },
        )

    def recover_orphaned_runs(
        self, end_reason: str = "runtime_restarted"
    ) -> list[ConversationRunRecord]:
        """启动时取消失去执行者的 running Run；suspended 没有执行者、不需要收敛，保留等消息唤醒。
        每个 Run 的状态与工具占位同事务提交；message_queue 保留，不自动继续，不发布尚无订阅者的事件。
        配对依据不完整时不补占位，记录错误并提交取消，继续恢复其他 Run。"""

        recovered: list[ConversationRunRecord] = []
        for run in self._run.list_recoverable():
            repaired_tool_call_ids: list[str] = []
            with self._session_factory.begin() as session:
                record = self._ledger.transition(
                    run.id,
                    RunTransition.CANCEL,
                    end_reason=end_reason,
                    session=session,
                )
                if record is not None:
                    try:
                        repaired_tool_call_ids = self._context.close_unclosed_tool_calls_for_run(
                            run.task_id, run.id, session=session
                        )
                    except ContextProtocolError as error:
                        # 配对计划在任何补写前失败；保留坏 context，仍提交取消并恢复其他 Run。
                        log.error(
                            "run_recovery_context_protocol_invalid",
                            extra={
                                "msg": "上下文配对依据不完整，保留原记录并取消遗留 Run",
                                "data": {
                                    "task_id": run.task_id,
                                    "run_id": run.id,
                                    **error.log_data(),
                                },
                            },
                        )
            if record is None:
                continue
            recovered.append(record)
            log.info(
                "run_status_persisted",
                extra={
                    "msg": "遗留 Run 已收敛为取消",
                    "data": {
                        "task_id": run.task_id,
                        "run_id": record.id,
                        "status": ConversationRunStatus.CANCELLED.value,
                        "end_reason": end_reason,
                        "repaired_tool_call_count": len(repaired_tool_call_ids),
                    },
                },
            )
            for tool_call_id in repaired_tool_call_ids:
                log.info(
                    "tool_observation_persisted",
                    extra={
                        "msg": "orphan Run 的未闭合工具调用已补取消占位",
                        "data": {
                            "task_id": run.task_id,
                            "run_id": record.id,
                            "tool_call_id": tool_call_id,
                            "status": ConversationRunStatus.CANCELLED.value,
                        },
                    },
                )
        return recovered
