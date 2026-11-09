"""委派与分叉的后台结果作业：``delegate_task`` 和 ``fork_task`` 共用。

作业以父调用的 ``tool_call_id`` 登记在 ``background_registry``，回投由 ``start_background_job`` 完成。结论不靠
子 Run 记下「是哪次调用触发了我」：投递之后在事件循环上等子 Task 无事可做
（``TaskActor.wait_quiescent``），再读它最新 Run 的 run model——子 Agent 的结论一定写在那里。父 Agent
连发几条输入时，它们并入同一个 Run 或接续出新 Run，几次调用的等待在同一次落定时醒来、读到同一个
最新 Run，结论覆盖了全部输入。所以子 Task 与 actor 都不知道委派的存在；用户在子 Task 里直接发送时
没有人在等，结论不回投。

子 Agent 一律后台执行：没有 ``background`` 参数与前台等待，父 Agent 可以显式等待，也会在模型准备结束时等待未结占位——委派
常常一次发出好几个，前台等待会占住工具线程。撤销即取消等待，并以 ``parent`` 原因停止子 Task。

等待只在进程内，不跨重启恢复：重启只把占位推到「已中断」，父 Run 不唤醒、不收结论，等它下次被驱动
时由首驱通知说明。子 Task 本身照常推进，只是结论不再回投。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from typing import cast

from app.config.logging.logger import log
from app.core.tool.background.background_job import BackgroundConclusion, BackgroundJob
from app.core.tool.background.background_registry import background_registry
from app.core.tool.background.run import start_background_job
from app.core.tool.schemas import ToolExecutionContext, ToolObservation
from app.models import ConversationRunRecord


def child_run_result(child_task_id: int, run: ConversationRunRecord) -> dict[str, object]:
    """子 Run 回投父 Agent 的结论正文。"""
    return {
        "child_task_id": child_task_id,
        "status": run.status,
        "end_reason": run.end_reason,
        "final_output": run.final_output,
    }


def run_child(
    execution_context: ToolExecutionContext,
    child_task_id: int,
    *,
    label: str,
    enqueue: Callable[[], None],
    placeholder: Callable[[], ToolObservation],
) -> ToolObservation:
    """登记后台作业、投递消息、开始等子 Task 落定，返回标 ``background`` 的占位（不挂起）。

    等待必须在投递之后开始：投递之前子 Task 往往已无事可做，会立刻读到上一次的结论。``enqueue``
    抛出时收回登记（工作没启动，不调撤销回调）并继续上抛。调用方保证 ``event_loop`` 可用。
    """
    tool_call_id = execution_context.tool_call_id
    job, waiting = _child_job(child_task_id)
    work = start_background_job(
        job,
        task_id=execution_context.task_id,
        run_id=execution_context.run_id,
        tool_call_id=tool_call_id,
        kind="child_agent",
        label=label,
    )
    try:
        enqueue()
    except Exception:
        background_registry.discard(execution_context.task_id, tool_call_id)
        raise
    _start_waiting(
        job, waiting, child_task_id, tool_call_id,
        cast(asyncio.AbstractEventLoop, execution_context.event_loop),
    )
    observation = placeholder()
    observation.background = work
    return observation


def _child_job(child_task_id: int) -> tuple[BackgroundJob, list[Future[None]]]:
    """等子 Task 结论的作业：撤销即取消等待，并以 ``parent`` 原因停止子 Task。"""
    from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

    waiting: list[Future[None]] = []

    def cancel() -> None:
        for future in waiting:
            future.cancel()
        task_runtime_spaces.get_or_create(child_task_id).actor.request_cancel("parent")

    job = BackgroundJob(
        on_cancel=cancel,
        cancelled_result=BackgroundConclusion({"child_task_id": child_task_id, "status": "cancelled"}),
    )
    return job, waiting


def _start_waiting(
    job: BackgroundJob,
    waiting: list[Future[None]],
    child_task_id: int,
    tool_call_id: str,
    loop: asyncio.AbstractEventLoop,
) -> None:
    waiting.append(
        asyncio.run_coroutine_threadsafe(
            _deliver_conclusion(job, child_task_id, tool_call_id), loop
        )
    )


async def _deliver_conclusion(job: BackgroundJob, child_task_id: int, tool_call_id: str) -> None:
    """等子 Task 无事可做，读最新 Run 的结论交给作业；交付会写 SQLite，放进工作线程。

    被撤销时协程随之取消，作业已以取消结论收尾。等待失败（子 Task 被删除、调度器已停止、读库失败）
    记录后以失败结论收尾，父 Agent 不会一直等下去。
    """
    from app.service.depends import get_task_service
    from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

    try:
        actor = task_runtime_spaces.get_or_create(child_task_id).actor
        if not await actor.wait_quiescent():
            # 子 Task 的调度器已停止：它不会再推进，读到的只会是上一次的 Run，不能当成本次结论。
            raise RuntimeError(f"task {child_task_id} 的调度器已停止，无法等待子 Agent 结论")
        run = await asyncio.to_thread(get_task_service().require_current_run, child_task_id)
        result = child_run_result(child_task_id, cast(ConversationRunRecord, run))
    except Exception:
        log.exception(
            "child_conclusion_wait_failed",
            extra={
                "msg": "等待子 Agent 结论失败",
                "data": {"child_task_id": child_task_id, "tool_call_id": tool_call_id},
            },
        )
        result = {"child_task_id": child_task_id, "status": "failed", "end_reason": "unavailable"}
    await asyncio.to_thread(job.finish, BackgroundConclusion(result))
