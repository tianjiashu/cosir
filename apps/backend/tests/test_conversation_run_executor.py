"""ConversationRunExecutor 单元测试。"""

import asyncio
from datetime import UTC, datetime

import pytest

from app.models import ConversationRunRecord
from app.assistant_transport.service.conversation_run_executor import ConversationRunExecutor


def _turn(run_id: int) -> ConversationRunRecord:
    """构造最小可执行轮次。"""

    now = datetime.now(UTC)
    return ConversationRunRecord(run_id, 1, "hello", "pending", now, now)


class _Reader:
    """测试用 turn 查询端口。"""

    def __init__(self, turn: ConversationRunRecord) -> None:
        self.turn = turn

    def get_run(self, run_id: int) -> ConversationRunRecord:
        """返回测试轮次。"""

        if run_id != self.turn.id:
            raise KeyError(run_id)
        return self.turn

    def claim_pending_run(self, run_id: int) -> bool:
        """模拟 pending run 的原子启动。"""
        if run_id != self.turn.id or self.turn.status != "pending":
            return False
        self.turn.status = "running"
        return True


@pytest.mark.asyncio
async def test_start_runs_in_background_and_reaches_completed() -> None:
    """start 不阻塞调用方，并在 runner 完成后报告 completed。"""

    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(turn: ConversationRunRecord) -> None:
        """等待测试信号后返回。"""

        assert turn.id == 7
        started.set()
        await release.wait()

    executor = ConversationRunExecutor(run_service=_Reader(_turn(7)))
    task = await executor.start(7, runner)
    await started.wait()
    assert (await executor.get_status(7)).status == "running"
    release.set()
    await task
    assert await executor.get_status(7) is None


@pytest.mark.asyncio
async def test_cancel_is_explicit_and_does_not_cancel_other_run() -> None:
    """显式取消只影响目标 run，且状态最终为 cancelled。"""

    blocker = asyncio.Event()

    async def runner(turn: ConversationRunRecord) -> None:
        """持续等待，模拟运行中的 Agent。"""

        await blocker.wait()

    executor = ConversationRunExecutor(run_service=_Reader(_turn(8)))
    task = await executor.start(8, runner)
    await asyncio.sleep(0)
    assert await executor.cancel(8) is True
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await executor.get_status(8) is None
    assert await executor.cancel(8) is False


@pytest.mark.asyncio
async def test_duplicate_active_run_is_rejected() -> None:
    """同一 run 不允许并发启动两个后台 task。"""

    blocker = asyncio.Event()

    async def runner(turn: ConversationRunRecord) -> None:
        """持续等待，保持 run 活动。"""

        await blocker.wait()

    executor = ConversationRunExecutor(run_service=_Reader(_turn(9)))
    task = await executor.start(9, runner)
    with pytest.raises(ValueError):
        await executor.start(9, runner)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_start_claims_pending_run_before_running() -> None:
    """执行器必须先原子启动 pending run，才能启动 Agent。"""

    reader = _Reader(_turn(10))
    ran = asyncio.Event()

    async def runner(turn: ConversationRunRecord) -> None:
        """记录已原子启动后才会执行。"""
        ran.set()

    executor = ConversationRunExecutor(run_service=reader)
    task = await executor.start(10, runner)
    await task
    assert ran.is_set()
