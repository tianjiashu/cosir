"""驱动任务的生命周期长于执行登记：注销执行者之后，释放回调（汇报、接续）仍在同一个任务里跑。

潜在缺陷：等待落定与进程关闭只看登记，登记一注销就当作已结束——停止或删除不等收尾就往下走，
进程关闭时留下没人取消的任务。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.core import session_execution as execution_module
from app.core.session_execution import SessionExecution
from app.core.workflow.execution_state.run_mailbox import RunMailbox


class _InstantDriver:
    async def drive(self, _run, _preceding) -> None:
        return None


def _execution_in_release(monkeypatch) -> tuple[SessionExecution, asyncio.Event, asyncio.Event]:
    """启动一次立即结束的驱动，让释放回调停在闸门上；返回执行登记、「回调已进入」与闸门。"""
    monkeypatch.setattr(execution_module, "RunDriver", _InstantDriver)
    entered, gate = asyncio.Event(), asyncio.Event()

    async def on_released(_run, _reason) -> None:
        entered.set()
        await gate.wait()

    execution = SessionExecution(9, RunMailbox(), on_released)
    execution.start(SimpleNamespace(id=1), [], None)
    return execution, entered, gate


async def test_waiting_for_release_covers_the_callback_after_deregistration(monkeypatch) -> None:
    execution, entered, gate = _execution_in_release(monkeypatch)
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert not execution.driving  # 已注销，回调还在跑

    waiting = asyncio.create_task(execution.wait_released())
    await asyncio.sleep(0.05)
    assert not waiting.done()
    gate.set()
    await asyncio.wait_for(waiting, timeout=2)


async def test_abort_cancels_the_callback_still_running(monkeypatch) -> None:
    execution, entered, _gate = _execution_in_release(monkeypatch)
    await asyncio.wait_for(entered.wait(), timeout=2)
    [task] = execution._tasks

    await asyncio.wait_for(execution.abort(), timeout=2)

    assert task.cancelled()
    assert not execution._tasks
