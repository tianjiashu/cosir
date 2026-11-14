"""撤回回合：移出最新一个由用户输入开启的回合（及之后的回合），并收拢它留下的全部副产物。

「撤回」的语义只在这里定义；编辑是前端把撤回的内容放回输入框再照常发送，后端没有编辑命令。
撤回不通知模型。回合的范围按行上的回合号（``turn``）算，副产物按调用归属（``tool_call_id``）找：

- 保留：更早发起的调用在这一回合才并入的结果（它们回答的是之前的调用）；
- 重新投递：这一回合途中并入的 Session 级通知（排队投递、只投一次，见 ``NoticeKind``）；推导出的
  回合级与 Run 级通知不入队，撤回后由下一次驱动重新推导；
- 撤销：这一回合发起、仍在进行的后台作业与交接；这一回合派生的 Session（委派、Agent fork，按
  ``SessionExtra.deriving_call_id`` 找）以 ``parent`` 原因收回；
- 删除：队列里属于这些调用的结论与决定，以及这一回合派生的 Session 已经交来、还没并入的汇报；
- 不处理：已经发生的外部事实——文件改动、终端会话、浏览器标签、派生 Session 已经做完的事、已发出的
  消息（见 AGENTS.md 待办）。

新增一种副产物时，只要它能挂到调用 id 上，就在 :meth:`TurnWithdrawalService.withdraw` 里加一处。
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.orm import Session as DbSession

from app.config.logging.logger import log
from app.core.tool.handoff.handoff_registry import handoff_registry
from app.core.workflow.message_queue.message_bodies.notice import NoticeBody, NoticeKind
from app.core.workflow.message_queue.session_message import (
    AgentMessageBody,
    BackgroundResultBody,
    DecisionBody,
    SessionMessage,
)
from app.models.errors.session_command_errors import SessionCommandConflictError
from app.models.session_context import SessionContextRecord
from app.models.withdrawal_plan import WithdrawalPlan
from app.service import depends as service_depends
from app.service.conversation.session_context_service import (
    SessionContextService,
)
from app.storage.crud.session_context_crud import SessionContextCrud
from app.storage.store_engines import main_db_factory
from app.storage.write_transaction import begin_immediate
from app.tools.tool_handler.terminal.command_results import command_results


class TurnWithdrawalService:
    """撤回回合的用例；调用方持 Session 闸门且已确认 Session 静止（没有执行者）。"""

    def __init__(self, context: SessionContextService) -> None:
        self._context = context
        self._crud = SessionContextCrud()

    def withdraw(self, session_id: int, message_id: int) -> None:
        """撤回 ``message_id`` 这条用户输入开启的回合及之后的全部回合；不开新 Run。

        顺序有讲究：

        1. 先在写事务之外撤销进程内与自带事务的副产物——后台作业（等这个 Session 的在途投递写完，见
           ``command_results.withdraw_calls``）、交接、这一回合派生的 Session。它们必须在移出提交之前
           完成（登记出队是投递的唯一门槛，被收回的 Session 不再汇报），又不能放进写事务：在途投递要写库。
        2. 再在一个 ``BEGIN IMMEDIATE`` 写事务里复查目标、移出行、删除队列里回答被撤回调用的行、
           重新投递 Session 级通知，同一事务提交。

        Session 静止且持闸门时没有别的写入方改这个 Session 的上下文，第 1 步之前读到的范围在第 2 步仍然
        成立；复查目标只防御调用方漏了静止判定。第 1 步不可回滚：事务随后失败时撤销不恢复，同外部
        工具副作用，被撤回的占位已不会再结算。

        异常:
            SessionCommandConflictError: ``WITHDRAW_TARGET_STALE``（目标不是最新一条用户输入）。
        """
        target = self._require_target(session_id, message_id, db=None)
        plan = WithdrawalPlan.plan_withdrawal(
            self._crud.get(session_id, include_in_context=False), target.turn
        )
        command_results.withdraw_calls(session_id, plan.calls)
        handoff_registry.drop(session_id, plan.calls)
        recalled = self._recall_derived(session_id, plan.calls)
        queue = service_depends.get_session_message_queue()
        with begin_immediate(main_db_factory()) as db:
            self._require_target(session_id, message_id, db=db)
            self._crud.exclude_rows(session_id, plan.excluded_row_ids, db)
            removed = queue.withdraw_matching(
                db,
                session_id,
                lambda message: self._answers_withdrawn_call(message, plan.calls)
                or self._reports_from(message, recalled),
            )
            # 撤回只在 Session 静止时接受，没有活动 Run 可唤醒：直接写外部队列、不经 actor，随下一次推进并入。
            for notice in plan.redeliver:
                queue.put(
                    session_id,
                    SessionMessage.of(
                        NoticeBody(
                            content=notice.content,
                            notice_kind=cast(NoticeKind, notice.notice_kind),
                        )
                    ),
                    db=db,
                )
        # 移出行不发工具事件：被撤回的等待占位（交接）要另外让 Session 状态推送重读一次。
        service_depends.get_session_feed().publish_status(session_id)
        log.info(
            "turn_withdrawn",
            extra={
                "msg": "撤回了最新回合",
                "data": {
                    "session_id": session_id,
                    "message_id": message_id,
                    "from_turn": target.turn,
                    "excluded_rows": len(plan.excluded_row_ids),
                    "withdrawn_calls": sorted(plan.calls),
                    "recalled_derived_sessions": sorted(recalled),
                    "removed_queue_messages": [message.kind for message in removed],
                    "redelivered_notices": [notice.notice_kind for notice in plan.redeliver],
                },
            },
        )

    def _require_target(
        self, session_id: int, message_id: int, *, db: DbSession | None
    ) -> SessionContextRecord:
        target = self._context.latest_user_input(session_id, db=db)
        if target is None or target.message_id != message_id:
            raise SessionCommandConflictError(
                "WITHDRAW_TARGET_STALE", "只能撤回最新一条用户消息，请刷新后重试"
            )
        return target

    @staticmethod
    def _recall_derived(session_id: int, calls: frozenset[str]) -> frozenset[int]:
        """以 ``parent`` 原因收回这一回合派生的 Session，返回它们的 id；只发起停止、不等待。

        收回在移出提交之前：被收回的 Run 以 ``parent_cancelled`` 落定，不再汇报。已经落定、汇报已入队的
        由调用方在同一写事务里按来源删掉。
        """
        from app.session_runtime.session_runtime_space_registry import session_runtime_spaces

        derived = [
            session
            for session in service_depends.get_session_service().list_derived_sessions(session_id)
            if session.extra.deriving_call_id in calls
        ]
        for session in derived:
            session_runtime_spaces.get_or_create(session.id).actor.request_cancel("parent")
        return frozenset(session.id for session in derived)

    @staticmethod
    def _reports_from(message: SessionMessage, session_ids: frozenset[int]) -> bool:
        """队列里来自这些派生 Session 的汇报。"""
        body = message.body
        if not isinstance(body, AgentMessageBody) or body.source.report is None:
            return False
        return body.source.session_id in session_ids

    def _answers_withdrawn_call(self, message: SessionMessage, calls: frozenset[str]) -> bool:
        """队列里回答被撤回调用的行：这些调用的结论与决定。队列里的通知都是 Session 级，照常保留。"""
        body = message.body
        return isinstance(body, BackgroundResultBody | DecisionBody) and body.tool_call_id in calls
