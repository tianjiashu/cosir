"""后端重启恢复用例：重启后的收敛与收尾只在这里按序编排，lifespan 只调用一次入口。"""

from collections.abc import Iterable

from app.config.logging.logger import log
from app.core.context.tool_call_closure import ContextProtocolError
from app.core.tool.background import cancel_placeholders
from app.core.workflow.message_queue.message_bodies import NoticeBody
from app.core.workflow.message_queue.task_message import BackgroundResultBody, TaskMessage
from app.models import (
    ConversationRunRecord,
    ConversationRunStatus,
)
from app.models.enums.conversation_run_status import RunTransition
from app.service import depends as service_depends
from app.storage.store_engines import main_session_factory
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

# 重启后告诉 Agent 发生了什么：后台工作与终端会话都不跨重启。
INTERRUPTED_NOTICE = (
    "The backend restarted. All interactive terminal sessions you opened before are gone, "
    "background commands were stopped, delegated waits were cancelled, and unfinished tool "
    "calls were interrupted. Re-run what you still need."
)


class RecoverService:
    """后端启动期的重启恢复：收敛遗留 running Run / TeamRun，拉起待调度 Task，结算中断的占位。

    不动 suspended Run 与 message_queue，不隐式重放旧执行，也不为重启生成结论消息或唤醒 Run。
    """

    def __init__(self) -> None:
        """绑定恢复所需的 Run CRUD、状态迁移、Task context 与事务工厂。"""

        self._run = service_depends.get_conversation_run_crud()
        self._ledger = service_depends.get_conversation_run_ledger()
        self._context = service_depends.get_conversation_task_context_service()
        self._session_factory = main_session_factory()

    def recover_after_restart(self) -> None:
        """重启恢复的唯一入口。

        调用方须已 ``task_runtime_spaces.bind_loop`` 并打开 ``background_registry``：本入口会
        物化 space（拉起 actor）。

        顺序即契约：

        1. 收敛遗留 Run 与 TeamRun，再取消作废 TeamRun 下挂起的节点 Run（没有 Coordinator 再等它，
           不能被之后的结论唤醒后独自继续）：之后查出的待调度 Task 以收敛后的 Run 状态为准。
        2. 为外部队列有待处理消息或当前 Run 挂起的 Task 物化 space（随之拉起 actor），让重启前
           已到达的消息照常处理、挂起的 Run 照常等唤醒。
        3. 把重启前还在等结果的占位推到「已中断」，再给受影响的 Task 各投一条重启通知：通知不
           ``WAKES``，不唤醒、不推进任何 Run，只随它们下次驱动并入上下文。
        """

        # coordinator 消费已装配的工具系统，且与 service 层互相引用，惰性导入。
        from app.core.tool.multi_agent.agent_team.team_coordinator import get_agent_team_coordinator

        recovered_runs = self.recover_orphaned_runs()
        orphan_node_task_ids = get_agent_team_coordinator().recover_after_restart()
        cancelled_node_run_ids = self.cancel_orphaned_team_nodes(orphan_node_task_ids)
        dispatch_task_ids = service_depends.get_task_service().list_task_ids_to_dispatch()
        for task_id in dispatch_task_ids:
            task_runtime_spaces.get_or_create(task_id)
        interrupted_task_ids = {
            run.task_id for run in recovered_runs
        } | self.interrupt_placeholders(dispatch_task_ids)
        self.notify_interrupted(interrupted_task_ids)
        log.info(
            "backend_restart_recovered",
            extra={
                "msg": "后端启动期重启恢复完成",
                "data": {
                    "cancelled_run_ids": [run.id for run in recovered_runs],
                    "cancelled_team_node_run_ids": cancelled_node_run_ids,
                    "dispatch_task_ids": sorted(dispatch_task_ids),
                    "interrupted_task_ids": sorted(interrupted_task_ids),
                },
            },
        )

    def interrupt_placeholders(self, task_ids: Iterable[int]) -> frozenset[int]:
        """把重启前还在等结果的占位推到「已中断」，返回受影响的 Task。

        ``task_ids`` 是启动时的候选 Task（已覆盖全部队列有消息的 Task），用来核对「结论已在队列里」
        的调用：那些占位不在这里改，由那条结论结算，否则卡片先显示已中断、结论又无处落。后台占位
        （``pending``）与 HITL 占位（``running``，决定已接收、执行被重启打断）都只改卡片状态，
        重启不再为它们生成结论消息。
        """

        queue = service_depends.get_task_message_queue()
        queued = frozenset(
            body.tool_call_id
            for task_id in task_ids
            for message in queue.pending_nowait(task_id)
            if isinstance(body := message.body, BackgroundResultBody) and body.tool_call_id is not None
        )
        return placeholders.cancel_placeholders(
            None, None, placeholders.INTERRUPTED_HINT, exclude=queued
        )

    def notify_interrupted(self, task_ids: Iterable[int]) -> None:
        """给被中断的 Task 各投一条重启通知，等它们下次驱动时并入。

        直接写外部队列而不经 ``task_runtime_spaces.enqueue``：不为一条不推进的通知物化 space、拉起
        常驻 actor。通知不 ``WAKES``，``dispatch_policy`` 因此不唤醒挂起的 Run、也不开新 Run，启动
        调度同样不把它算作待处理消息。
        """

        queue = service_depends.get_task_message_queue()
        for task_id in sorted(set(task_ids)):
            queue.put(
                task_id,
                TaskMessage.of(
                    NoticeBody(content=INTERRUPTED_NOTICE, notice_kind="backend_restarted")
                ),
            )

    def cancel_orphaned_team_nodes(
        self, node_task_ids: list[int], end_reason: str = "runtime_restarted"
    ) -> list[int]:
        """取消作废 TeamRun 下挂起的节点 Run，返回被取消的 Run id。

        只对「等待方不可能再出现」的节点这样做：一般的挂起 Run 照常保留、等可唤醒消息。单个失败
        记录后继续。
        """
        tasks = service_depends.get_task_service()
        cancelled: list[int] = []
        for task_id in node_task_ids:
            try:
                run = tasks.require_current_run(task_id)
                if run is None or run.status != ConversationRunStatus.SUSPENDED.value:
                    continue
                if self._ledger.transition(run.id, RunTransition.CANCEL, end_reason=end_reason):
                    cancelled.append(run.id)
            except Exception:
                log.exception(
                    "orphan_team_node_cancel_failed",
                    extra={"msg": "取消作废 TeamRun 的挂起节点失败", "data": {"task_id": task_id}},
                )
        return cancelled

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
