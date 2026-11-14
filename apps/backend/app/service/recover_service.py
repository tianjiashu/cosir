"""后端重启恢复用例：重启后的收敛与收尾只在这里按序编排，lifespan 只调用一次入口。"""

from collections.abc import Iterable

from app.config.logging.logger import log
from app.core.context.tool_call_closure import ContextProtocolError
from app.core.tool.placeholder_settlement import (
    INTERRUPTED_HINT,
    cancel_placeholders,
    expire_turn_decisions_after_run,
)
from app.core.workflow.message_queue.message_bodies import NoticeBody
from app.core.workflow.message_queue.session_message import BackgroundResultBody, SessionMessage
from app.models import (
    RunRecord,
    RunStatus,
)
from app.models.enums.run_status import RunTransition
from app.service import depends as service_depends
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces
from app.storage.store_engines import main_db_factory

# 重启后告诉 Agent 发生了什么：后台工作与终端会话都不跨重启。
INTERRUPTED_NOTICE = (
    "The backend restarted. All interactive terminal sessions you opened before are gone, "
    "background commands were stopped, delegated child agents and forks that were still "
    "working were stopped and will not report, and unfinished tool calls were interrupted. "
    "Re-run what you still need."
)


# 重启收敛给残留 Run 记的结束原因；重启通知与前端都按它识别「被重启中断」。
_RUNTIME_RESTARTED = "runtime_restarted"

class RecoverService:
    """后端启动期的重启恢复：收敛遗留 running Run，拉起待调度 Session，结算中断的占位。

    不动 suspended Run 与 message_queue，不隐式重放旧执行，也不为重启生成结论消息或唤醒 Run。
    """

    def __init__(self) -> None:
        """绑定恢复所需的 Run CRUD、状态迁移、Session context 与事务工厂。"""

        self._run = service_depends.get_run_crud()
        self._ledger = service_depends.get_run_ledger()
        self._context = service_depends.get_session_context_service()
        self._db_factory = main_db_factory()

    def recover_after_restart(self) -> None:
        """重启恢复的唯一入口。

        调用方须已 ``session_runtime_spaces.bind_loop`` 并打开 ``command_results``：本入口会
        物化 space（拉起 actor）。

        顺序即契约：

        1. 收敛遗留 Run：之后查出的待调度 Session 以收敛后的 Run 状态为准。
        2. 为外部队列有待处理消息或当前 Run 挂起的 Session 物化 space（随之拉起 actor），让重启前
           已到达的消息照常处理、挂起的 Run 照常等唤醒。
        3. 被收敛的 Run 留下的提问作废；把重启前还在等结果的占位推到「已中断」，再给受影响的
           Session 各投一条重启通知：它只唤醒挂起的 Run（在 ``wait_messages`` 上等的工作已经没了，
           不叫醒就一直挂着），不开新 Run，空闲的 Session 随下次驱动并入。
        """

        recovered_runs = self.recover_orphaned_runs()
        dispatch_session_ids = service_depends.get_session_service().list_session_ids_to_dispatch()
        for session_id in dispatch_session_ids:
            session_runtime_spaces.get_or_create(session_id)
        recovered_session_ids = {run.session_id for run in recovered_runs}
        # 被收敛的 Run 不经 actor 的落定点：它们留下的回合类等待决定（提问）在这里作废，模型从下面
        # 的重启通知得知。挂起的 Run 不在其列，它的提问照常等回答。
        for session_id in sorted(recovered_session_ids):
            expire_turn_decisions_after_run(session_id, INTERRUPTED_HINT)
        interrupted_session_ids = (
            recovered_session_ids
            | self._derivers_of(recovered_session_ids)
            | self.interrupt_placeholders(dispatch_session_ids)
        )
        self.notify_interrupted(interrupted_session_ids)
        log.info(
            "backend_restart_recovered",
            extra={
                "msg": "后端启动期重启恢复完成",
                "data": {
                    "cancelled_run_ids": [run.id for run in recovered_runs],
                    "dispatch_session_ids": sorted(dispatch_session_ids),
                    "interrupted_session_ids": sorted(interrupted_session_ids),
                },
            },
        )

    @staticmethod
    def _derivers_of(session_ids: Iterable[int]) -> set[int]:
        """被中断的派生 Session 的派生方：重启收敛不经 actor 的落定点、不汇报，派生方只能从通知得知。

        fork 不随来源删除，来源可能已不在：跳过。
        """
        sessions = service_depends.get_session_service()
        derivers: set[int] = set()
        for session_id in session_ids:
            deriver_id = sessions.get_session(session_id).deriver_id()
            if deriver_id is None:
                continue
            try:
                sessions.get_session(deriver_id)
            except KeyError:
                continue
            derivers.add(deriver_id)
        return derivers

    def interrupt_placeholders(self, session_ids: Iterable[int]) -> frozenset[int]:
        """把重启前还在等结果的占位推到「已中断」，返回受影响的 Session。

        ``session_ids`` 是启动时的候选 Session（已覆盖全部队列有消息的 Session），用来核对「结论已在队列里」
        的调用：那些占位不在这里改，由那条结论结算，否则卡片先显示已中断、结论又无处落。后台占位
        （``pending``）与 HITL 占位（``running``，决定已接收、执行被重启打断）都只改卡片状态，
        重启不再为它们生成结论消息。
        """

        queue = service_depends.get_session_message_queue()
        queued = frozenset(
            (session_id, body.tool_call_id)
            for session_id in session_ids
            for message in queue.pending_nowait(session_id)
            if isinstance(body := message.body, BackgroundResultBody)
        )
        return cancel_placeholders(None, None, INTERRUPTED_HINT, exclude=queued)

    def notify_interrupted(self, session_ids: Iterable[int]) -> None:
        """给被中断的 Session 各投一条重启通知，等它们下次驱动时并入。

        直接写外部队列而不经 ``session_runtime_spaces.enqueue``：通知不开新 Run，不为它给空闲 Session
        物化 space、拉起常驻 actor；当前 Run 挂起的 Session 已在第 2 步拉起 actor，入队即被它读到并唤醒。
        """

        queue = service_depends.get_session_message_queue()
        for session_id in sorted(set(session_ids)):
            queue.put(
                session_id,
                SessionMessage.of(
                    NoticeBody(content=INTERRUPTED_NOTICE, notice_kind="backend_restarted")
                ),
            )

    def recover_orphaned_runs(self) -> list[RunRecord]:
        """启动时取消失去执行者的 running Run；suspended 没有执行者、不需要收敛，保留等消息唤醒。
        每个 Run 的状态与工具占位同事务提交；message_queue 保留，不自动继续，不发布尚无订阅者的事件。
        配对依据不完整时不补占位，记录错误并提交取消，继续恢复其他 Run。"""

        recovered: list[RunRecord] = []
        for run in self._run.list_recoverable():
            repaired_tool_call_ids: list[str] = []
            with self._db_factory.begin() as db:
                record = self._ledger.transition(
                    run.id,
                    RunTransition.CANCEL,
                    end_reason=_RUNTIME_RESTARTED,
                    db=db,
                )
                if record is not None:
                    try:
                        repaired_tool_call_ids = self._context.close_unclosed_tool_calls_for_run(
                            run.session_id, run.id, db=db
                        )
                    except ContextProtocolError as error:
                        # 配对计划在任何补写前失败；保留坏 context，仍提交取消并恢复其他 Run。
                        log.error(
                            "run_recovery_context_protocol_invalid",
                            extra={
                                "msg": "上下文配对依据不完整，保留原记录并取消遗留 Run",
                                "data": {
                                    "session_id": run.session_id,
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
                        "session_id": run.session_id,
                        "run_id": record.id,
                        "status": RunStatus.CANCELLED.value,
                        "end_reason": _RUNTIME_RESTARTED,
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
                            "session_id": run.session_id,
                            "run_id": record.id,
                            "tool_call_id": tool_call_id,
                            "status": RunStatus.CANCELLED.value,
                        },
                    },
                )
        return recovered
