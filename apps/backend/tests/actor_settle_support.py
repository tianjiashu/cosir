"""测试辅助：等一个 Task「无事可做」再断言它的落定结果。

生产代码不提供「等 Task 静止」的通知，这里按短间隔轮询同一组事实：actor 空闲（无执行者、当前
Run 不在挂起）、当前 Run 不在运行、下游没有未了结的工作（``derived_report.has_open_work``），
且外部队列里没有能开新 Run 的消息——否则 actor 还会再推进一次，测试读到的是上一次的 Run。
"""

from __future__ import annotations

import asyncio

from app.core.tool.multi_agent import derived_report
from app.models import ConversationRunStatus
from app.service import depends
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

_POLL_INTERVAL = 0.02
_SETTLE_TIMEOUT = 8


def _settled_facts(task_id: int) -> bool:
    """闸门外同步读库的那几项事实（在线程里调用）。"""
    current = depends.get_task_service().require_current_run(task_id)
    if current is not None and current.status == ConversationRunStatus.RUNNING:
        return False
    if derived_report.has_open_work(task_id):
        return False
    pending = depends.get_task_message_queue().pending_nowait(task_id)
    return not any(message.wakes(None) for message in pending)


async def wait_task_settled(task_id: int) -> None:
    """轮询直到 Task 无事可做；``_SETTLE_TIMEOUT`` 秒内没等到抛 ``TimeoutError``。

    调用方须已物化该 Task 的 runtime space（``task_runtime_spaces.get`` 能取到 actor）。
    """
    async with asyncio.timeout(_SETTLE_TIMEOUT):
        while True:
            actor = task_runtime_spaces.get(task_id).actor
            if await actor.phase() == "idle" and await asyncio.to_thread(_settled_facts, task_id):
                return
            await asyncio.sleep(_POLL_INTERVAL)
