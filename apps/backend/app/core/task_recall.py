"""收回一个 Task 交出去的工作：撤销后台工作、取消活动 Team、收回子 Task。"""

from __future__ import annotations

import asyncio

from app.config.logging.logger import log
from app.core.tool.background.background_registry import background_registry
from app.core.tool.background.cancel_placeholders import RUN_STOPPED_HINT, cancel_placeholders
from app.service import depends as service_depends


class TaskRecall:
    """上游以 ``parent`` 原因停止一个 Task 时，它交出去的工作一并收回；由该 Task 的 ``TaskActor`` 持有。

    停止分两种语义：用户停止（``user``）只打断当前驱动，后台工作、Team 与子 Task 照常运行，结论到达时
    照常唤醒或开新 Run；上游停止（``parent``：撤销委派、Coordinator 停节点、收回向下传播）收回这个 Task
    的一切，本类就是「收回」那一半。是否收回、何时收回归 actor：有执行者时在打信号之后，挂起的 Run 在
    迁移之前，空闲时直接收回。删除不经这里：删除先停下整棵所有权子树，Team 由删除入口排空，后台工作在
    删除提交后撤销。

    本类不持进程内状态，只读写各下游的事实源（后台结果登记表、TeamRun、子 Task）。
    """

    def __init__(self, task_id: int) -> None:
        self.task_id = task_id

    async def recall(self) -> None:
        """撤销全部后台工作、取消全部活动 Team（待确认与运行中）、以 ``parent`` 原因收回子 Task；不抛异常。

        调用方是取消入口，这里一抛，Run 就停不下来；各步失败只记录。子 Task 只发起停止、不等待：调用方
        持自己的 Task 闸门，等子 Task 的闸门会让父子互等。子 Task 的收回经 actor 同样走到这里，所以整棵
        所有权子树逐层收回。
        """
        await self._drop_background()
        try:
            await service_depends.get_agent_team_coordinator().cancel_for_parent_task(self.task_id)
        except Exception:
            log.exception(
                "task_recall_teams_failed",
                extra={"msg": "收回时取消 Agent Team 失败", "data": {"task_id": self.task_id}},
            )
        from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

        try:
            children = await asyncio.to_thread(
                service_depends.get_task_service().list_child_tasks, self.task_id
            )
            for child in children:
                task_runtime_spaces.get_or_create(child.id).actor.request_cancel("parent")
        except Exception:
            log.exception(
                "task_recall_children_failed",
                extra={"msg": "收回子 Task 失败", "data": {"task_id": self.task_id}},
            )

    async def _drop_background(self) -> None:
        """撤销本 Task 的全部后台条目（含更早 Run 登记的与前台等待），被撤销的后台卡片推到 ``cancelled``。

        前台条目没有占位行，工具线程会自己以取消结果收尾。回调可能取终端服务的锁并触发快照刷新，放进工作
        线程；回调异常由 ``drop_task`` 逐条记录。撤销登记本身不失败；占位只是卡片展示状态，结算写库失败
        可以接受——残留的 ``pending`` 占位由重启收尾推到「已中断」。
        """
        background = await asyncio.to_thread(background_registry.drop_task, self.task_id)
        if not background:
            return
        try:
            await asyncio.to_thread(
                cancel_placeholders,
                self.task_id,
                background,
                RUN_STOPPED_HINT,
                displays=background,
            )
        except Exception:
            log.exception(
                "background_placeholder_settle_failed",
                extra={
                    "msg": "收回时结算后台占位失败，占位留待重启收尾",
                    "data": {"task_id": self.task_id, "tool_call_ids": list(background)},
                },
            )
