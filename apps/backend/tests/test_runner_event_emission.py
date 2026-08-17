"""``AgentRuntime`` 事件发射路径（落库 / 广播 / 变更集稳定通知）的单元测试。

通过 ``object.__new__`` 绕过 ``AgentRuntime.__init__`` 的 service 单例装配，注入 mock
``RuntimeEventService`` 与 ``FileSnapshotCrud``，聚焦验证 5 个私有协作方法的真实行为：

- ``_emit``：落库失败降级透传、成功返回 stamped、非 RuntimeError 冒泡、loop 线程安全发布。
- ``_publish_runtime_event``：无 loop 直发、有 loop 走 ``call_soon_threadsafe``。
- ``_save_and_publish_runtime_event``：先落库后广播；落库失败不广播并冒泡。
- ``_publish_stable_file_changes``：只查询一次快照、逐条广播、空集不广播、失败不冒泡。

这些测试锁定现有行为契约，作为后续重构的回归护栏。
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.core.agents.agent_profile import AgentProfile
from app.core.runtime.runner import AgentRuntime
from app.models.enums.event_type import EventType
from app.models.event.runtime_event import RuntimeEvent
from app.models.file_snapshot_record import FileSnapshotRecord
from app.models.payload.file_change_stable_payload import FileChangeStablePayload
from app.service.agent_runtime_event.runtime_event_service import RuntimeEventService


def _make_runner(service: RuntimeEventService) -> AgentRuntime:
    """构造绕过 __init__ 的 AgentRuntime，仅注入 mock 事件服务。"""

    runner = object.__new__(AgentRuntime)
    runner._runtime_event_service = service
    return runner


def _make_event(task_id: str = "t-task", turn_id: str = "t-turn") -> RuntimeEvent:
    """构造一个 FILE_CHANGE_STABLE 运行时事件。"""

    return RuntimeEvent(
        event_type=EventType.FILE_CHANGE_STABLE,
        task_id=task_id,
        turn_id=turn_id,
        payload=FileChangeStablePayload(
            task_id=task_id,
            turn_id=turn_id,
            path="a.txt",
            action="modified",
        ),
    )


def _make_profile(loop: asyncio.AbstractEventLoop | None = None) -> AgentProfile:
    """构造测试用 AgentProfile（默认无 runtime_event_loop）。

    用 ``object.__new__`` 绕过 dataclass ``__init__``：避免触发 ``_default_workflow()``
    的 react 模块导入（工作区存在未完成的 ``context_usage_meter`` 重构，导入链当前不可用）。
    ``_emit`` 仅读取 ``runtime_event_loop`` 字段，无需完整初始化。
    """

    profile = object.__new__(AgentProfile)
    profile.runtime_event_loop = loop
    return profile


class TestEmit:
    """``_emit`` 落库 + 透传行为。"""

    async def test_persist_success_returns_stamped_and_publishes_it(self) -> None:
        """落库成功：返回 save_event 的结果，并把它广播给订阅者。"""

        service = MagicMock()
        stamped = _make_event()
        service.save_event.return_value = stamped
        runner = _make_runner(service)

        result = await runner._emit(_make_event(), _make_profile())

        assert result is stamped
        service.publish_event.assert_called_once_with(stamped)

    async def test_persist_failure_falls_back_to_original_and_still_publishes(self) -> None:
        """落库抛 RuntimeError：降级透传原事件，广播仍执行，不中断运行。"""

        service = MagicMock()
        service.save_event.side_effect = RuntimeError("persist boom")
        runner = _make_runner(service)
        event = _make_event()

        result = await runner._emit(event, _make_profile())

        assert result is event
        service.publish_event.assert_called_once_with(event)

    async def test_non_runtime_error_propagates_without_publishing(self) -> None:
        """落库抛非 RuntimeError（如 ValueError）：异常向上冒泡，不广播。"""

        service = MagicMock()
        service.save_event.side_effect = ValueError("unexpected")
        runner = _make_runner(service)

        with pytest.raises(ValueError):
            await runner._emit(_make_event(), _make_profile())

        service.publish_event.assert_not_called()

    async def test_publish_uses_profile_loop_when_present(self) -> None:
        """agent_profile 携带 loop：走 call_soon_threadsafe，不直接调 publish_event。"""

        service = MagicMock()
        loop = MagicMock()
        runner = _make_runner(service)
        event = _make_event()
        service.save_event.return_value = event

        await runner._emit(event, _make_profile(loop=loop))

        loop.call_soon_threadsafe.assert_called_once_with(service.publish_event, event)
        service.publish_event.assert_not_called()

    async def test_profile_loop_closed_propagates_broadcast_error(self) -> None:
        """广播阶段 loop 已关闭（call_soon_threadsafe 抛 RuntimeError）：异常向上冒泡。

        docstring 契约：落库异常被吞并降级，但广播阶段 loop 失效属于调用方需感知的
        异常路径，不应被静默吞掉。
        """

        service = MagicMock()
        loop = MagicMock()
        loop.call_soon_threadsafe.side_effect = RuntimeError("loop closed")
        runner = _make_runner(service)
        event = _make_event()
        service.save_event.return_value = event

        with pytest.raises(RuntimeError):
            await runner._emit(event, _make_profile(loop=loop))


class TestPublishRuntimeEvent:
    """``_publish_runtime_event`` 的 loop 分流行为。"""

    def test_without_loop_publishes_directly(self) -> None:
        """无 loop：直接调用 publish_event。"""

        service = MagicMock()
        runner = _make_runner(service)
        event = _make_event()

        runner._publish_runtime_event(event)

        service.publish_event.assert_called_once_with(event)

    def test_with_loop_uses_call_soon_threadsafe(self) -> None:
        """有 loop：经 call_soon_threadsafe 调度发布，不直接 publish。"""

        service = MagicMock()
        loop = MagicMock()
        runner = _make_runner(service)
        event = _make_event()

        runner._publish_runtime_event(event, loop)

        loop.call_soon_threadsafe.assert_called_once_with(service.publish_event, event)
        service.publish_event.assert_not_called()


class TestSaveAndPublishRuntimeEvent:
    """``_save_and_publish_runtime_event`` 的落库 + 广播组合行为。"""

    def test_persists_then_publishes_stamped(self) -> None:
        """先落库拿到 stamped，再广播 stamped 并返回它。"""

        service = MagicMock()
        stamped = _make_event()
        service.save_event.return_value = stamped
        runner = _make_runner(service)

        result = runner._save_and_publish_runtime_event(_make_event())

        service.save_event.assert_called_once()
        service.publish_event.assert_called_once_with(stamped)
        assert result is stamped

    def test_persist_failure_skips_publish_and_propagates(self) -> None:
        """落库抛 RuntimeError：不广播，异常向上冒泡。"""

        service = MagicMock()
        service.save_event.side_effect = RuntimeError("persist boom")
        runner = _make_runner(service)

        with pytest.raises(RuntimeError):
            runner._save_and_publish_runtime_event(_make_event())

        service.publish_event.assert_not_called()

    def test_with_loop_forwards_to_call_soon_threadsafe(self) -> None:
        """带 loop：落库后经 call_soon_threadsafe 广播，不直接 publish。"""

        service = MagicMock()
        stamped = _make_event()
        service.save_event.return_value = stamped
        loop = MagicMock()
        runner = _make_runner(service)

        result = runner._save_and_publish_runtime_event(_make_event(), loop)

        loop.call_soon_threadsafe.assert_called_once_with(service.publish_event, stamped)
        service.publish_event.assert_not_called()
        assert result is stamped


class TestPublishStableFileChanges:
    """``_publish_stable_file_changes`` 的快照收口与广播行为。"""

    @patch("app.core.runtime.runner.FileSnapshotCrud")
    async def test_marks_stable_once_queries_once_and_broadcasts_each(
        self, crud_cls: MagicMock
    ) -> None:
        """标记 stable 一次、查询快照一次、逐条广播 file_change_stable 事件。"""

        service = MagicMock()
        runner = _make_runner(service)
        crud = crud_cls.return_value
        crud.list_stable_by_task.return_value = [
            FileSnapshotRecord(path="a.txt", action="modified"),
            FileSnapshotRecord(path="b.txt", action="created"),
        ]

        await runner._publish_stable_file_changes("t-task", "t-turn", is_main_agent=True)

        crud.mark_stable_by_turn.assert_called_once_with("t-turn")
        crud.list_stable_by_task.assert_called_once_with("t-task", ["t-turn"])
        service.save_event.assert_not_called()
        assert service.publish_event.call_count == 2
        calls = service.publish_event.call_args_list
        assert [c.args[0].payload.path for c in calls] == ["a.txt", "b.txt"]
        assert [c.args[0].payload.action for c in calls] == ["modified", "created"]
        for call in calls:
            event = call.args[0]
            assert event.task_id == "t-task"
            assert event.turn_id == "t-turn"
            assert event.event_type is EventType.FILE_CHANGE_STABLE
            assert event.is_main_agent is True

    @patch("app.core.runtime.runner.FileSnapshotCrud")
    async def test_no_snapshots_no_broadcast(self, crud_cls: MagicMock) -> None:
        """无稳定快照：查询一次后直接返回，不广播。"""

        service = MagicMock()
        runner = _make_runner(service)
        crud_cls.return_value.list_stable_by_task.return_value = []

        await runner._publish_stable_file_changes("t-task", "t-turn", is_main_agent=False)

        crud_cls.return_value.list_stable_by_task.assert_called_once_with("t-task", ["t-turn"])
        service.save_event.assert_not_called()
        service.publish_event.assert_not_called()

    @patch("app.core.runtime.runner.FileSnapshotCrud")
    async def test_broadcasts_is_main_agent_false(self, crud_cls: MagicMock) -> None:
        """is_main_agent=False 透传到广播事件（区分主/子 Agent 变更）。"""

        service = MagicMock()
        runner = _make_runner(service)
        crud_cls.return_value.list_stable_by_task.return_value = [
            FileSnapshotRecord(path="a.txt", action="modified"),
        ]

        await runner._publish_stable_file_changes("t-task", "t-turn", is_main_agent=False)

        event = service.publish_event.call_args.args[0]
        assert event.is_main_agent is False

    @patch("app.core.runtime.runner.FileSnapshotCrud")
    async def test_mark_stable_failure_does_not_raise(self, crud_cls: MagicMock) -> None:
        """标记 stable 抛异常：被 _mark_stable_file_changes 吞掉，不冒泡不广播。"""

        service = MagicMock()
        runner = _make_runner(service)
        crud_cls.return_value.mark_stable_by_turn.side_effect = RuntimeError("mark boom")
        crud_cls.return_value.list_stable_by_task.return_value = []

        await runner._publish_stable_file_changes("t-task", "t-turn", is_main_agent=True)

        service.publish_event.assert_not_called()

    @patch("app.core.runtime.runner.FileSnapshotCrud")
    @patch("app.core.runtime.runner.log")
    async def test_query_failure_logs_warning_without_raising(
        self, log: MagicMock, crud_cls: MagicMock
    ) -> None:
        """查询抛异常：记 warning 不冒泡，不广播。"""

        service = MagicMock()
        runner = _make_runner(service)
        crud_cls.return_value.list_stable_by_task.side_effect = RuntimeError("db boom")

        await runner._publish_stable_file_changes("t-task", "t-turn", is_main_agent=True)

        assert log.warning.called
        service.publish_event.assert_not_called()

    @patch("app.core.runtime.runner.FileSnapshotCrud")
    @patch("app.core.runtime.runner.log")
    async def test_broadcast_failure_logs_warning_without_raising(
        self, log: MagicMock, crud_cls: MagicMock
    ) -> None:
        """广播抛异常：被外层 except 吞掉，记 warning 不冒泡。"""

        service = MagicMock()
        service.publish_event.side_effect = RuntimeError("publish boom")
        runner = _make_runner(service)
        crud_cls.return_value.list_stable_by_task.return_value = [
            FileSnapshotRecord(path="a.txt", action="modified"),
        ]

        await runner._publish_stable_file_changes("t-task", "t-turn", is_main_agent=True)

        assert log.warning.called
