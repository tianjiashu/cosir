"""撤回回合：移出最新一个由用户输入开启的回合（及之后的回合），并收拢它留下的全部副产物。

「撤回」的语义只在这里定义；编辑是前端把撤回的内容放回输入框再照常发送，后端没有编辑命令。
撤回不通知模型。回合的范围按行上的回合号（``turn``）算，副产物按调用归属（``tool_call_id``）找：

- 保留：更早发起的调用在这一回合才并入的结果（它们回答的是之前的调用）；
- 重新投递：这一回合途中并入的 Task 级通知（排队投递、只投一次，见 ``NoticeKind``）；推导出的
  回合级与 Run 级通知不入队，撤回后由下一次驱动重新推导；
- 撤销：这一回合发起、仍在进行的后台作业，交接，这一回合提出的 Agent Team 方案（待确认的收敛，运行中
  的停下且不汇报）；这一回合派生的 Task（委派、Agent fork，按 ``TaskExtra.deriving_call_id`` 找）以
  ``parent`` 原因收回；
- 删除：队列里属于这些调用的结论与决定，以及这一回合派生的 Task 与 Team 已经交来、还没并入的汇报；
- 不处理：已经发生的外部事实——文件改动、终端会话、浏览器标签、派生 Task 已经做完的事、已发出的
  消息（见 AGENTS.md 待办）。

新增一种副产物时，只要它能挂到调用 id 上，就在 :meth:`TurnWithdrawalService.withdraw` 里加一处。
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.orm import Session

from app.config.logging.logger import log
from app.core.tool.handoff.handoff_registry import handoff_registry
from app.core.workflow.message_queue.message_bodies.notice import NoticeBody, NoticeKind
from app.core.workflow.message_queue.task_message import (
    AgentMessageBody,
    BackgroundResultBody,
    DecisionBody,
    TaskMessage,
)
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.errors.task_command_errors import TaskCommandConflictError
from app.models.withdrawal_plan import WithdrawalPlan
from app.service import depends as service_depends
from app.service.conversation.conversation_task_context_service import (
    ConversationTaskContextService,
)
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
from app.storage.store_engines import main_session_factory
from app.storage.write_transaction import begin_immediate
from app.tools.tool_handler.terminal.command_results import command_results


class TurnWithdrawalService:
    """撤回回合的用例；调用方持 Task 闸门且已确认 Task 静止（没有执行者）。"""

    def __init__(self, context: ConversationTaskContextService) -> None:
        self._context = context
        self._crud = ConversationTaskContextCrud()

    def withdraw(self, task_id: int, message_id: int) -> None:
        """撤回 ``message_id`` 这条用户输入开启的回合及之后的全部回合；不开新 Run。

        顺序有讲究：

        1. 先在写事务之外撤销进程内与自带事务的副产物——后台作业（等这个 Task 的在途投递写完，见
           ``command_results.withdraw_calls``）、交接、这一回合的 Team 与派生 Task。它们必须在移出
           提交之前完成（登记出队是投递的唯一门槛，被收回的 Team 与 Task 不再汇报），又不能放进写事务：
           在途投递与 Team 收敛都要写库。
        2. 再在一个 ``BEGIN IMMEDIATE`` 写事务里复查目标、移出行、删除队列里回答被撤回调用的行、
           重新投递 Task 级通知，同一事务提交。

        Task 静止且持闸门时没有别的写入方改这个 Task 的上下文，第 1 步之前读到的范围在第 2 步仍然
        成立；复查目标只防御调用方漏了静止判定。第 1 步不可回滚：事务随后失败时撤销不恢复，同外部
        工具副作用，被撤回的占位已不会再结算。

        异常:
            TaskCommandConflictError: ``WITHDRAW_TARGET_STALE``（目标不是最新一条用户输入）。
        """
        target = self._require_target(task_id, message_id, session=None)
        plan = WithdrawalPlan.plan_withdrawal(
            self._crud.get(task_id, include_in_context=False), target.turn
        )
        command_results.withdraw_calls(task_id, plan.calls)
        handoff_registry.drop(task_id, plan.calls)
        recalled = self._recall_derived(task_id, plan.calls)
        withdrawn_teams = service_depends.get_agent_team_coordinator().withdraw_for_calls(
            task_id, plan.calls
        )
        queue = service_depends.get_task_message_queue()
        with begin_immediate(main_session_factory()) as session:
            self._require_target(task_id, message_id, session=session)
            self._crud.exclude_rows(task_id, plan.excluded_row_ids, session)
            removed = queue.withdraw_matching(
                session,
                task_id,
                lambda message: self._answers_withdrawn_call(message, plan.calls)
                or self._reports_from(message, recalled, withdrawn_teams),
            )
            # 撤回只在 Task 静止时接受，没有活动 Run 可唤醒：直接写外部队列、不经 actor，随下一次推进并入。
            for notice in plan.redeliver:
                queue.put(
                    task_id,
                    TaskMessage.of(
                        NoticeBody(
                            content=notice.content,
                            notice_kind=cast(NoticeKind, notice.notice_kind),
                        )
                    ),
                    session=session,
                )
        # 移出行不发工具事件：被撤回的等待占位（Team 方案、交接）要另外让 Task 状态推送重读一次。
        service_depends.get_task_feed().publish_status(task_id)
        log.info(
            "turn_withdrawn",
            extra={
                "msg": "撤回了最新回合",
                "data": {
                    "task_id": task_id,
                    "message_id": message_id,
                    "from_turn": target.turn,
                    "excluded_rows": len(plan.excluded_row_ids),
                    "withdrawn_calls": sorted(plan.calls),
                    "withdrawn_teams": sorted(withdrawn_teams),
                    "recalled_derived_tasks": sorted(recalled),
                    "removed_queue_messages": [message.kind for message in removed],
                    "redelivered_notices": [notice.notice_kind for notice in plan.redeliver],
                },
            },
        )

    def _require_target(
        self, task_id: int, message_id: int, *, session: Session | None
    ) -> ConversationTaskContextRecord:
        target = self._context.latest_user_input(task_id, session=session)
        if target is None or target.message_id != message_id:
            raise TaskCommandConflictError(
                "WITHDRAW_TARGET_STALE", "只能撤回最新一条用户消息，请刷新后重试"
            )
        return target

    @staticmethod
    def _recall_derived(task_id: int, calls: frozenset[str]) -> frozenset[int]:
        """以 ``parent`` 原因收回这一回合派生的 Task，返回它们的 id；只发起停止、不等待。

        收回在移出提交之前：被收回的 Run 以 ``parent_cancelled`` 落定，不再汇报。已经落定、汇报已入队的
        由调用方在同一写事务里按来源删掉。
        """
        from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

        derived = [
            task
            for task in service_depends.get_task_service().list_derived_tasks(task_id)
            if task.extra.deriving_call_id in calls
        ]
        for task in derived:
            task_runtime_spaces.get_or_create(task.id).actor.request_cancel("parent")
        return frozenset(task.id for task in derived)

    @staticmethod
    def _reports_from(
        message: TaskMessage, task_ids: frozenset[int], team_run_ids: frozenset[int]
    ) -> bool:
        """队列里来自这些派生 Task 或 TeamRun 的汇报。"""
        body = message.body
        if not isinstance(body, AgentMessageBody) or body.source.report is None:
            return False
        return body.source.task_id in task_ids or body.source.team_run_id in team_run_ids

    def _answers_withdrawn_call(self, message: TaskMessage, calls: frozenset[str]) -> bool:
        """队列里回答被撤回调用的行：这些调用的结论与决定。队列里的通知都是 Task 级，照常保留。"""
        body = message.body
        return isinstance(body, BackgroundResultBody | DecisionBody) and body.tool_call_id in calls
