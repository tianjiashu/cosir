"""委派子 Agent 的延迟结果作业：``delegate_task`` 与开新 Run 的 ``child_agent_send`` 共用。

作业以父调用的 ``tool_call_id`` 登记在 ``deferred_registry``，回投由 ``run_deferred`` 完成。结论不靠
子 Run 记下「是哪次调用触发了我」：投递之后在事件循环上等子 Task 无事可做
（``TaskActor.wait_quiescent``），再读它最新 Run 的 run model——子 Agent 的结论一定写在那里。父 Agent
连发几条输入时，它们并入同一个 Run 或接续出新 Run，几次调用的等待在同一次落定时醒来、读到同一个
最新 Run，结论覆盖了全部输入。所以子 Task 与 actor 都不知道委派的存在；用户在子 Task 里直接发送时
没有人在等，结论不回投。

子 Agent 一律后台执行：没有 ``background`` 参数与前台等待，父 Agent 要等结论就调用 ``wait``——委派
常常一次发出好几个，前台等待会占住工具线程。撤销即取消等待，并以 ``parent`` 原因停止子 Task。

等待只在进程内，重启后不重建：被恢复流程收敛的子 Run 由 ``restart_settlement`` 直接回投父 Task。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from typing import cast

from app.config.logging.logger import log
from app.core.tool.background.deferred_job import ManualJob
from app.core.tool.background.deferred_registry import deferred_registry
from app.core.tool.background.run_deferred import run_deferred
from app.core.tool.schemas import ToolExecutionContext, ToolObservation
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.models import ConversationRunRecord


def child_run_result(child_task_id: int, run: ConversationRunRecord) -> dict[str, object]:
    """子 Run 回投父 Agent 的结论正文；重启收尾（``restart_settlement``）用同一形状。"""
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
    enqueue: Callable[[], None],
    placeholder: Callable[[], ToolObservation],
) -> ToolObservation:
    """登记后台作业、投递消息、开始等子 Task 落定，返回标 ``background`` 的占位（不挂起）。

    等待必须在投递之后开始：投递之前子 Task 往往已无事可做，会立刻读到上一次的结论。``enqueue``
    抛出时收回登记（工作没启动，不调撤销回调）并继续上抛。调用方保证 ``event_loop`` 可用。
    """
    from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

    tool_call_id = execution_context.tool_call_id
    loop = cast(asyncio.AbstractEventLoop, execution_context.event_loop)
    waiting: list[Future[None]] = []

    def cancel() -> None:
        for future in waiting:
            future.cancel()
        task_runtime_spaces.get_or_create(child_task_id).actor.request_cancel("parent")

    job = ManualJob(
        on_cancel=cancel,
        cancelled_result={"child_task_id": child_task_id, "status": "cancelled"},
    )
    run_deferred(
        job,
        task_id=execution_context.task_id,
        run_id=execution_context.run_id,
        tool_call_id=tool_call_id,
        kind="child_agent",
        label=f"child_task:{child_task_id}",
        background=True,
        foreground_limit_s=None,
        is_cancelled=lambda: cancellation_registry.is_cancelled(execution_context.task_id),
    )
    try:
        enqueue()
    except Exception:
        deferred_registry.discard(tool_call_id)
        raise
    waiting.append(
        asyncio.run_coroutine_threadsafe(
            _deliver_conclusion(job, child_task_id, tool_call_id), loop
        )
    )
    observation = placeholder()
    observation.deferred = True
    return observation


async def _deliver_conclusion(job: ManualJob, child_task_id: int, tool_call_id: str) -> None:
    """等子 Task 无事可做，读最新 Run 的结论交给作业；交付会写 SQLite，放进工作线程。

    被撤销时协程随之取消，作业已以取消结论收尾。等待失败（子 Task 被删除、读库失败）记录后以失败
    结论收尾，父 Agent 不会一直等下去。
    """
    from app.service.depends import get_task_service
    from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

    try:
        await task_runtime_spaces.get_or_create(child_task_id).actor.wait_quiescent()
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
    await asyncio.to_thread(job.finish, result)
