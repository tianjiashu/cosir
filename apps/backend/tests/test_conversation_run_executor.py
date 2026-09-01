"""ConversationRunExecutor 单元测试。"""

import asyncio
from datetime import UTC, datetime

import pytest

from app.models import TurnRecord
from app.service.task.conversation_run_executor import ConversationRunExecutor


def _turn(turn_id: int) -> TurnRecord:
    """构造最小可执行轮次。"""

    now = datetime.now(UTC)
    return TurnRecord(turn_id, 1, "hello", "pending", now, now)


class _Reader:
    """测试用 turn 查询端口。"""

    def __init__(self, turn: TurnRecord) -> None:
        self.turn = turn

    def get_turn(self, turn_id: int) -> TurnRecord:
        """返回测试轮次。"""

        if turn_id != self.turn.id:
            raise KeyError(turn_id)
        return self.turn


class _LeaseReader(_Reader):
    """带租约认领记录的测试读取端口。"""

    def __init__(self, turn: TurnRecord) -> None:
        super().__init__(turn)
        self.claims: list[tuple[int, str]] = []

    def claim_executor_lease(self, turn_id: int, owner: str, lease_seconds: int = 60) -> TurnRecord:
        """记录一次条件认领并返回 fencing 快照。"""
        self.claims.append((turn_id, owner))
        self.turn.fencing_version = 1
        return self.turn


@pytest.mark.asyncio
async def test_start_runs_in_background_and_reaches_completed() -> None:
    """start 不阻塞调用方，并在 runner 完成后报告 completed。"""

    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(turn: TurnRecord) -> None:
        """等待测试信号后返回。"""

        assert turn.id == 7
        started.set()
        await release.wait()

    executor = ConversationRunExecutor(_Reader(_turn(7)))
    task = await executor.start(7, runner)
    await started.wait()
    assert (await executor.get_status(7)).status == "running"
    release.set()
    await task
    assert (await executor.get_status(7)).status == "completed"


@pytest.mark.asyncio
async def test_cancel_is_explicit_and_does_not_cancel_other_run() -> None:
    """显式取消只影响目标 run，且状态最终为 cancelled。"""

    blocker = asyncio.Event()

    async def runner(turn: TurnRecord) -> None:
        """持续等待，模拟运行中的 Agent。"""

        await blocker.wait()

    executor = ConversationRunExecutor(_Reader(_turn(8)))
    task = await executor.start(8, runner)
    await asyncio.sleep(0)
    assert await executor.cancel(8) is True
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await executor.get_status(8)).status == "cancelled"
    assert await executor.cancel(8) is False


@pytest.mark.asyncio
async def test_duplicate_active_run_is_rejected() -> None:
    """同一 run 不允许并发启动两个后台 task。"""

    blocker = asyncio.Event()

    async def runner(turn: TurnRecord) -> None:
        """持续等待，保持 run 活动。"""

        await blocker.wait()

    executor = ConversationRunExecutor(_Reader(_turn(9)))
    task = await executor.start(9, runner)
    with pytest.raises(ValueError):
        await executor.start(9, runner)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_start_claims_persistent_executor_lease_before_running() -> None:
    """执行器必须先取得持久化 lease，才能启动 Agent。"""

    reader = _LeaseReader(_turn(10))
    ran = asyncio.Event()

    async def runner(turn: TurnRecord) -> None:
        """记录已取得 lease 后才会执行。"""
        assert turn.fencing_version == 1
        ran.set()

    executor = ConversationRunExecutor(reader)
    task = await executor.start(10, runner)
    await task
    assert ran.is_set()
    assert len(reader.claims) == 1
    assert reader.claims[0][0] == 10
