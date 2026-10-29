"""Task 取消的副作用与向下游传播：取消运行中的 Agent Team、撤销延迟结果、停止子 Task、留用户取消通知。"""

from __future__ import annotations

import asyncio

from app.config.logging.logger import log
from app.core.tool.background import placeholders
from app.core.tool.background.deferred_registry import deferred_registry
from app.core.workflow.cancel.conversation_run_cancellation_registry import CancelReason
from app.core.workflow.message_queue.task_message import NoticeBody, TaskMessage
from app.models import ConversationRunRecord
from app.service import depends as service_depends


class TaskCancellation:
    """一个 Task 取消时要对下游做的事；由该 Task 的 ``TaskActor`` 持有，并在固定时机调用。

    只承载「取消生效前后对外做什么」，不判定是否取消、何时生效：取消入口、取消信号的打出与取走、
    挂起 Run 的迁移都归 actor——它们依赖 actor 的执行登记与 Task 闸门。本类不持进程内状态，只读写
    各下游的事实源（TeamRun、延迟结果登记表、子 Task、外部队列）。调用时机：

    - :meth:`cancel_teams`：发起停止那一刻——有执行者时由 ``TaskExecution.cancel``、挂起的 Run 由
      actor 迁移前调用；
    - :meth:`drop_deferred_entries`：取消「生效」之前（执行收尾取走信号前、挂起 Run 迁移前）；
    - :meth:`after_cancelled`：Run 落定 cancelled 之后。
    """

    def __init__(self, task_id: int) -> None:
        self.task_id = task_id

    async def cancel_teams(self) -> None:
        """取消本 Task 的全部活动 TeamRun（待确认与运行中）；失败只记录。

        ``agent_team`` 工具在线程里阻塞运行 Team，执行中途不检查取消信号：等 Run 落定再取消 Team
        会互等。Coordinator 检出 TeamRun 已取消就停下节点并返回，工具随之结束，Run 才能走到检查点。
        按 Task 寻址的理由见 ``cancel_for_parent_task``。
        """
        from app.core.tool.handler.agent_team.coordinator import get_agent_team_coordinator

        try:
            await asyncio.to_thread(get_agent_team_coordinator().cancel_for_parent_task, self.task_id)
        except Exception:
            log.exception(
                "task_team_cancel_failed",
                extra={"msg": "停止时取消 Agent Team 失败", "data": {"task_id": self.task_id}},
            )

    async def cancel_background_task(self) -> None:
        """撤销本 Task 的全部延迟结果（含更早 Run 登记的；后台命令、终端交接）：之后到达的完成不再
        投递，交接状态由撤销回调清除。回调可能取终端服务的锁并触发快照刷新，放进工作线程；回调异常
        由 ``drop_task`` 逐条记录，不会抛到这里。

        必须在取消「生效」之前调用，否则两步之间到达的结论会入队并开出新 Run。被撤销的后台条目随即
        把占位行推到 ``cancelled``；前台条目没有占位行，工具线程会自己以取消结果收尾。

        不抛异常：调用方是取消收尾（执行释放前、挂起 Run 迁移前），这里一抛，信号取不走、执行不注销，
        Task 就永久卡在停止中。撤销登记是必须完成的那一步，它本身不失败；占位只是卡片展示状态，
        结算写库失败可以接受——记录后照常收尾，残留的 ``pending`` 占位由重启收尾推到「已中断」。
        """
        dropped = await asyncio.to_thread(deferred_registry.drop_task, self.task_id)
        background = [entry.tool_call_id for entry in dropped if entry.mode == "background"]
        if not background:
            return
        try:
            await asyncio.to_thread(
                placeholders.settle_placeholders,
                self.task_id,
                background,
                "cancelled",
                placeholders.RUN_STOPPED_HINT,
            )
        except Exception:
            log.exception(
                "deferred_placeholder_settle_failed",
                extra={
                    "msg": "取消时结算后台占位失败，照常收尾；占位留待重启收尾",
                    "data": {"task_id": self.task_id, "tool_call_ids": background},
                },
            )

    async def after_cancelled(self, run: ConversationRunRecord, reason: CancelReason) -> None:
        """Run 落定 cancelled 后：向子 Task 传播，用户取消再给下一个 Run 留一条通知。

        延迟结果与 Agent Team 不在这里：前者须在取消生效前撤销，后者在发起停止时已取消。子 Task 一律
        以 ``parent`` 原因停止，只发起、不等待：调用方可能持自己的闸门，去等子 Task 的闸门会让父子
        互等。通知以 ``next_run`` 入队、不推进，随下一次推进写入；上游停止不留通知。传播失败
        只记录，不影响本 Run 终态；通知入队失败上抛给调用方记录。
        """
        from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

        try:
            children = await asyncio.to_thread(
                service_depends.get_task_service().list_child_tasks, self.task_id
            )
            for child in children:
                task_runtime_spaces.get_or_create(child.id).actor.request_cancel("parent")
        except Exception:
            log.exception(
                "task_cancel_propagation_failed",
                extra={
                    "msg": "Run 取消后向子 Task 传播失败",
                    "data": {"task_id": self.task_id, "run_id": run.id},
                },
            )
        if reason != "user":
            return
        notice = TaskMessage.of(
            NoticeBody(content="The user stopped your previous turn before it finished.", notice_kind="user_cancelled"),
            delivery="next_run",
        )
        await asyncio.to_thread(
            service_depends.get_task_message_queue().put, self.task_id, notice
        )
