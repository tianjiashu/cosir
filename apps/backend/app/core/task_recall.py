"""收回一个 Task 交出去的工作：撤销后台工作、收回它派生的 Task。"""

from __future__ import annotations

import asyncio

from app.config.logging.logger import log
from app.core.tool.background_commands import background_commands
from app.core.tool.placeholder_settlement import RUN_STOPPED_HINT, cancel_placeholders
from app.service import depends as service_depends


class TaskRecall:
    """上游以 ``parent`` 原因停止一个 Task 时，它交出去的工作一并收回；由该 Task 的 ``TaskActor`` 持有。

    停止分两种语义：用户停止（``user``）只打断当前驱动，后台工作与子 Task 照常运行，结论到达时照常唤醒
    或开新 Run；上游停止（``parent``：撤销委派、撤回回合、收回向下传播）收回这个 Task 的一切，本类就是
    「收回」那一半。是否收回、何时收回归 actor：有执行者时在打信号之后，挂起的 Run 在迁移之前，空闲时
    直接收回。删除不经这里：删除先停下整棵所有权子树，后台工作在删除提交后撤销。

    撤销与结算是两步：撤销后台工作返回被撤销的调用，卡片结算由 actor 决定时机（见 :meth:`settle`）。
    本类不持进程内状态，只读写各下游的事实源（后台结果登记表、派生 Task）。
    """

    def __init__(self, task_id: int) -> None:
        self.task_id = task_id

    async def recall(self) -> dict[str, dict[str, object] | None]:
        """撤销全部后台工作、以 ``parent`` 原因收回派生 Task；不抛异常。

        返回被撤销的后台调用及其卡片终态展示，交给 :meth:`settle`；本方法不结算卡片。

        派生 Task 按派生关系找（委派的子 Agent 与 Agent fork，见 ``TaskService.list_derived_tasks``）：
        fork 不挂所有权子树，按 ``parent_task_id`` 找会漏掉它。调用方是取消入口，这里一抛，Run 就停不
        下来；各步失败只记录。派生 Task 只发起停止、不等待：调用方持自己的 Task 闸门，等对方的闸门会让
        双方互等。派生 Task 的收回经它的 actor 同样走到这里，所以逐层向下收回。被收回的 Run 以
        ``parent_cancelled`` 落定，不再向这里汇报。
        """
        revoked = await asyncio.to_thread(background_commands().recall_task, self.task_id)
        from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

        try:
            derived = await asyncio.to_thread(
                service_depends.get_task_service().list_derived_tasks, self.task_id
            )
            for task in derived:
                task_runtime_spaces.get_or_create(task.id).actor.request_cancel("parent")
        except Exception:
            log.exception(
                "task_recall_derived_failed",
                extra={"msg": "收回派生 Task 失败", "data": {"task_id": self.task_id}},
            )
        return revoked

    async def settle(self, revoked: dict[str, dict[str, object] | None]) -> None:
        """把被撤销的后台卡片推到 ``cancelled``（只改仍 ``pending`` 的占位，可重复调用）；不抛异常。

        占位行由 observe 在整批工具结束后写入，收回时同一批的行可能还没写，所以 actor 在驱动释放后会再
        调一次。占位只是卡片展示状态，结算写库失败可以接受——残留的 ``pending`` 占位由重启收尾推到
        「已中断」。前台条目没有占位行，工具线程会自己以取消结果收尾。
        """
        if not revoked:
            return
        try:
            await asyncio.to_thread(
                cancel_placeholders, self.task_id, revoked, RUN_STOPPED_HINT, displays=revoked,
            )
        except Exception:
            log.exception(
                "background_placeholder_settle_failed",
                extra={
                    "msg": "收回时结算后台占位失败，占位留待重启收尾",
                    "data": {"task_id": self.task_id, "tool_call_ids": list(revoked)},
                },
            )
