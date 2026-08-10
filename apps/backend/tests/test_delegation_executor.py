"""core delegation executor tests."""

from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.config.configuration import build_agent_registry
from app.core.agents.agent_profile import default_developer_agent
from app.core.delegation.child_agent_runner import ChildAgentRunner
from app.core.delegation.delegation_executor import DelegationExecutor
from app.core.runtime.runner import AgentRuntime
from app.core.runtime.turn_cancellation_registry import cancellation_registry
from app.models.delegation_record import DelegationRecord
from app.models.enums.event_type import EventType
from app.models.event.runtime_event import RuntimeEvent
from app.models.payload.final_response_payload import FinalResponsePayload
from app.models.payload.run_finished_payload import RunFinishedPayload
from app.models.turn_record import TurnRecord
from app.service.delegation.delegation_result import DelegationResult
from app.service.delegation.delegation_service import DelegationService
from app.tools.schemas import ToolExecutionContext
from app.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies
from app.tools.tool_models.delegate_task_args import DelegateTaskArgs
from app.utils.datetime_utils import utc_now


@dataclass
class FakeTaskRecord:
    """测试用任务记录。"""

    task_id: str = "task_1"
    workspace_id: str = "workspace_1"


class FakeDelegationService:
    """记录 executor 状态流转调用的 fake delegation service。"""

    def __init__(self, active_children: int = 0) -> None:
        """初始化 fake service。

        参数:
            active_children: 当前父 turn 下 pending/running 委派数量。

        返回:
            无。

        异常:
            无。

        副作用:
            初始化调用记录列表。
        """

        self.active_children = active_children
        self.calls: list[tuple[str, object]] = []
        self.created_delegation_id = ""

    def count_active_children(self, parent_turn_id: str) -> int:
        """返回指定 parent turn 的活跃 child 数量。

        参数:
            parent_turn_id: 父 turn 标识。

        返回:
            测试构造时指定的活跃 child 数量。

        异常:
            无。

        副作用:
            记录本次查询调用。
        """

        self.calls.append(("count_active_children", parent_turn_id))
        return self.active_children

    def create_pending(
        self,
        task_id: str,
        parent_turn_id: str,
        parent_agent_id: str,
        child_agent_id: str,
        delegation_type: str,
        prompt: str,
        requested_tools: tuple[str, ...],
        effective_tools: tuple[str, ...],
        runtime_event_loop=None,
    ) -> str:
        """记录 pending 委派并返回固定 delegation_id。

        参数:
            task_id: 所属任务标识。
            parent_turn_id: 父 turn 标识。
            parent_agent_id: 父 Agent 标识。
            child_agent_id: child Agent 标识。
            delegation_type: 委派类型。
            prompt: 委派指令。
            requested_tools: 请求工具集合。
            effective_tools: 策略收敛后的工具集合。

        返回:
            固定 delegation 标识。

        异常:
            无。

        副作用:
            记录 create_pending 调用及其关键参数。
        """

        self.created_delegation_id = "delegation_1"
        self.calls.append(
            (
                "create_pending",
                {
                    "task_id": task_id,
                    "parent_turn_id": parent_turn_id,
                    "parent_agent_id": parent_agent_id,
                    "child_agent_id": child_agent_id,
                    "delegation_type": delegation_type,
                    "prompt": prompt,
                    "requested_tools": requested_tools,
                    "effective_tools": effective_tools,
                    "runtime_event_loop": runtime_event_loop,
                },
            )
        )
        return self.created_delegation_id

    def mark_child_started(
        self,
        delegation_id: str,
        child_turn_id: str,
        runtime_event_loop=None,
    ) -> None:
        """记录 child turn 已开始。

        参数:
            delegation_id: 委派标识。
            child_turn_id: child turn 标识。

        返回:
            无。

        异常:
            无。

        副作用:
            记录 mark_child_started 调用。
        """

        self.calls.append(
            ("mark_child_started", (delegation_id, child_turn_id, runtime_event_loop))
        )

    def mark_completed(self, delegation_id: str, summary: str, runtime_event_loop=None) -> None:
        """记录委派成功终态。

        参数:
            delegation_id: 委派标识。
            summary: child 执行摘要。

        返回:
            无。

        异常:
            无。

        副作用:
            记录 mark_completed 调用。
        """

        self.calls.append(("mark_completed", (delegation_id, summary, runtime_event_loop)))

    def mark_failed(self, delegation_id: str, error: str, runtime_event_loop=None) -> None:
        """记录委派失败终态。

        参数:
            delegation_id: 委派标识。
            error: 失败原因。

        返回:
            无。

        异常:
            无。

        副作用:
            记录 mark_failed 调用。
        """

        self.calls.append(("mark_failed", (delegation_id, error, runtime_event_loop)))

    def mark_cancelled(self, delegation_id: str, error: str, runtime_event_loop=None) -> None:
        """记录委派取消终态。

        参数:
            delegation_id: 委派标识。
            error: 取消原因。

        返回:
            无。

        异常:
            无。

        副作用:
            记录 mark_cancelled 调用。
        """

        self.calls.append(("mark_cancelled", (delegation_id, error, runtime_event_loop)))


class FakeTurnService:
    """创建 child turn 的 fake turn service。"""

    def __init__(self) -> None:
        """初始化 fake turn service。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            初始化调用记录列表。
        """

        self.calls: list[tuple[str, object]] = []

    def create_child_turn(
        self,
        task_id: str,
        input_text: str,
        agent_id: str,
        parent_turn_id: str,
        delegation_id: str,
    ) -> TurnRecord:
        """创建测试用 child turn 记录。

        参数:
            task_id: 所属任务标识。
            input_text: child 输入文本。
            agent_id: child agent 标识。
            parent_turn_id: 父 turn 标识。
            delegation_id: 委派标识。

        返回:
            带父子字段的 pending TurnRecord。

        异常:
            无。

        副作用:
            记录 create_child_turn 调用。
        """

        self.calls.append(
            (
                "create_child_turn",
                {
                    "task_id": task_id,
                    "input_text": input_text,
                    "agent_id": agent_id,
                    "parent_turn_id": parent_turn_id,
                    "delegation_id": delegation_id,
                },
            )
        )
        return TurnRecord(
            turn_id="child_turn_1",
            task_id=task_id,
            input_text=input_text,
            status="pending",
            created_at=utc_now(),
            updated_at=utc_now(),
            agent_id=agent_id,
            parent_turn_id=parent_turn_id,
            delegation_id=delegation_id,
        )

    def claim_pending_turn(self, turn_id: str) -> bool:
        """记录 child turn 被 runtime 认领为 running。

        参数:
            turn_id: 待认领的 child turn 标识。

        返回:
            固定返回 True，表示认领成功。

        异常:
            无。

        副作用:
            记录 claim_pending_turn 调用。
        """

        self.calls.append(("claim_pending_turn", turn_id))
        return True


class FakeChildRunner:
    """返回预设委派结果并记录收到的 child profile。"""

    def __init__(self, result: DelegationResult) -> None:
        """初始化 fake child runner。

        参数:
            result: run_child 返回的预设结果。

        返回:
            无。

        异常:
            无。

        副作用:
            初始化调用记录。
        """

        self.result = result
        self.child_profiles = []

    def run_child(self, child_profile):
        """返回预设 child 执行结果。

        参数:
            child_profile: executor 派生出的本次 child AgentProfile。

        返回:
            预设 DelegationResult。

        异常:
            无。

        副作用:
            记录 child_profile。
        """

        self.child_profiles.append(child_profile)
        return self.result


class FakeDelegationCrud:
    """为 DelegationService 测试提供内存 CRUD。"""

    def __init__(self) -> None:
        """初始化内存 delegation 记录。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            初始化记录字典。
        """

        self.records = {}

    def create(self, record):
        """保存一条 delegation 记录。

        参数:
            record: 需要保存的 delegation 记录。

        返回:
            原样返回保存的记录。

        异常:
            无。

        副作用:
            写入内存记录字典。
        """

        self.records[record.delegation_id] = record
        return record

    def update_status(
        self,
        delegation_id: str,
        status: str,
        child_turn_id: str | None = None,
        summary: str | None = None,
        error: str | None = None,
    ):
        """更新内存 delegation 状态。

        参数:
            delegation_id: delegation 标识。
            status: 新状态。
            child_turn_id: 可选 child turn 标识。
            summary: 可选摘要。
            error: 可选错误。

        返回:
            更新后的 delegation 记录。

        异常:
            KeyError: 当 delegation 不存在时抛出。

        副作用:
            覆盖内存记录。
        """

        record = self.records[delegation_id]
        updated = replace(
            record,
            status=status,
            child_turn_id=child_turn_id if child_turn_id is not None else record.child_turn_id,
            summary=summary if summary is not None else record.summary,
            error=error if error is not None else record.error,
        )
        self.records[delegation_id] = updated
        return updated

    def list_by_parent_turn(self, parent_turn_id: str):
        """按 parent turn 返回内存 delegation 记录。

        参数:
            parent_turn_id: parent turn 标识。

        返回:
            匹配的 delegation 记录列表。

        异常:
            无。

        副作用:
            无。
        """

        return [
            record for record in self.records.values() if record.parent_turn_id == parent_turn_id
        ]


class FakeRuntimeEventService:
    """记录 runtime event save/publish 调用的 fake service。"""

    def __init__(self) -> None:
        """初始化调用记录。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            初始化事件记录列表。
        """

        self.saved = []
        self.published = []

    def save_event(self, event):
        """记录 save_event 调用。

        参数:
            event: 待保存的 runtime event。

        返回:
            原样返回事件。

        异常:
            无。

        副作用:
            写入 saved 列表。
        """

        self.saved.append(event)
        return event

    def publish_event(self, event) -> None:
        """记录 publish_event 调用。

        参数:
            event: 待发布的 runtime event。

        返回:
            无。

        异常:
            无。

        副作用:
            写入 published 列表。
        """

        self.published.append(event)


class FakeCascadeTurnService:
    """为 AgentRuntime.cancel_turn 级联取消测试提供 turn service。"""

    def __init__(self) -> None:
        """初始化 parent 与 child turn 状态。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            初始化内存 turn 字典和取消记录。
        """

        self.parent_turn = _parent_turn()
        self.child_turn = TurnRecord(
            turn_id="child_turn_1",
            task_id="task_1",
            input_text="child",
            status="running",
            created_at=utc_now(),
            updated_at=utc_now(),
            agent_id="delegate_reviewer",
            parent_turn_id=self.parent_turn.turn_id,
            delegation_id="delegation_1",
        )
        self.cancelled_turns = []

    def get_turn(self, turn_id: str) -> TurnRecord:
        """返回测试 turn。

        参数:
            turn_id: turn 标识。

        返回:
            parent 或 child turn 记录。

        异常:
            KeyError: 当 turn 不存在时抛出。

        副作用:
            无。
        """

        if turn_id == self.parent_turn.turn_id:
            return self.parent_turn
        if turn_id == self.child_turn.turn_id:
            return self.child_turn
        raise KeyError(turn_id)

    def cancel_turn_if_active(self, turn_id: str, end_reason: str) -> TurnRecord | None:
        """取消 active turn。

        参数:
            turn_id: turn 标识。
            end_reason: 取消原因。

        返回:
            更新后的 turn；如果已经非 active 则返回 None。

        异常:
            KeyError: 当 turn 不存在时抛出。

        副作用:
            记录取消调用并更新内存 turn。
        """

        turn = self.get_turn(turn_id)
        if turn.status not in {"pending", "running"}:
            return None
        cancelled = replace(turn, status="cancelled", end_reason=end_reason)
        if turn_id == self.parent_turn.turn_id:
            self.parent_turn = cancelled
        else:
            self.child_turn = cancelled
        self.cancelled_turns.append((turn_id, end_reason))
        return cancelled


class FakeCascadeDelegationService:
    """为 AgentRuntime.cancel_turn 级联取消测试提供 delegation service。"""

    def __init__(self) -> None:
        """初始化一条 running delegation。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            初始化记录与取消列表。
        """

        now = utc_now()
        self.record = DelegationRecord(
            delegation_id="delegation_1",
            task_id="task_1",
            parent_turn_id="parent_turn_1",
            child_turn_id="child_turn_1",
            parent_agent_id="developer",
            child_agent_id="delegate_reviewer",
            delegation_type="review",
            status="running",
            prompt="review",
            summary="",
            error="",
            requested_tools=("read_file",),
            effective_tools=("read_file",),
            created_at=now,
            updated_at=now,
        )
        self.cancelled = []

    def list_active_by_parent_turn(self, parent_turn_id: str):
        """返回指定 parent turn 下的 active delegation。

        参数:
            parent_turn_id: parent turn 标识。

        返回:
            active delegation 列表。

        异常:
            无。

        副作用:
            无。
        """

        if parent_turn_id == self.record.parent_turn_id and self.record.status == "running":
            return [self.record]
        return []

    def mark_cancelled(self, delegation_id: str, error: str) -> None:
        """记录 delegation cancelled 终态。

        参数:
            delegation_id: delegation 标识。
            error: 取消原因。

        返回:
            无。

        异常:
            无。

        副作用:
            更新内存记录并记录调用。
        """

        self.record = replace(self.record, status="cancelled", error=error)
        self.cancelled.append((delegation_id, error))


def _parent_turn(parent_turn_id: str | None = None) -> TurnRecord:
    """构造测试用 parent turn。

    参数:
        parent_turn_id: 可选的父 turn 标识，用于模拟 child turn 再委派。

    返回:
        测试用 TurnRecord。

    异常:
        无。

    副作用:
        无。
    """

    return TurnRecord(
        turn_id="parent_turn_1",
        task_id="task_1",
        input_text="parent",
        status="running",
        created_at=utc_now(),
        updated_at=utc_now(),
        agent_id="developer",
        parent_turn_id=parent_turn_id,
    )


def _execution_context(tmp_path: Path) -> ToolExecutionContext:
    """构造测试用工具执行上下文。

    参数:
        tmp_path: pytest 临时目录。

    返回:
        绑定 parent turn 的 ToolExecutionContext。

    异常:
        无。

    副作用:
        无。
    """

    return ToolExecutionContext(
        task_id="task_1",
        workspace_id="workspace_1",
        workspace_root=tmp_path,
        turn_id="parent_turn_1",
    )


@pytest.fixture
def executor_dependencies(tmp_path: Path) -> dict[str, object]:
    """构造 DelegationExecutor 的 fake 协作者。

    参数:
        tmp_path: pytest 临时目录。

    返回:
        可直接展开传入 DelegationExecutor 的依赖字典，附带 execution_context。

    异常:
        无。

    副作用:
        构建本地内存 registry 与 fake service。
    """

    parent_profile = replace(
        default_developer_agent(),
        allowed_tools=["read_file", "delegate_task"],
    )
    return {
        "agent_registry": build_agent_registry(),
        "delegation_service": FakeDelegationService(),
        "turn_service": FakeTurnService(),
        "child_runner": FakeChildRunner(
            DelegationResult(
                status="completed",
                child_turn_id="child_turn_1",
                summary="child done",
            )
        ),
        "parent_profile": parent_profile,
        "parent_turn": _parent_turn(),
        "parent_task": FakeTaskRecord(),
        "registered_tool_names": ("read_file", "delegate_task"),
        "execution_context": _execution_context(tmp_path),
    }


def test_delegation_executor_rejects_unknown_child(executor_dependencies):
    """验证未知 child profile 会在创建 delegation 前被拒绝。

    参数:
        executor_dependencies: fake executor 协作者集合。

    返回:
        无。

    异常:
        AssertionError: 当结果或副作用不符合预期时由 pytest 抛出。

    副作用:
        调用 DelegationExecutor.execute。
    """

    executor = DelegationExecutor(**executor_dependencies)
    result = executor.execute(
        DelegateTaskArgs(
            child_agent_id="missing_agent",
            delegation_type="review",
            prompt="review",
            requested_tools=["read_file"],
        ),
        execution_context=executor_dependencies["execution_context"],
    )

    assert result.status == "error"
    assert "unknown_child_agent" in result.content
    delegation_service = executor_dependencies["delegation_service"]
    assert not any(call[0] == "create_pending" for call in delegation_service.calls)


def test_delegation_executor_rejects_policy_denial_before_create(executor_dependencies):
    """验证策略拒绝不会创建 delegation 记录。

    参数:
        executor_dependencies: fake executor 协作者集合。

    返回:
        无。

    异常:
        AssertionError: 当结果或副作用不符合预期时由 pytest 抛出。

    副作用:
        调用 DelegationExecutor.execute。
    """

    executor_dependencies["parent_turn"] = _parent_turn(parent_turn_id="grand_parent")
    executor = DelegationExecutor(**executor_dependencies)
    result = executor.execute(
        DelegateTaskArgs(
            child_agent_id="delegate_reviewer",
            delegation_type="review",
            prompt="review",
            requested_tools=["read_file"],
        ),
        execution_context=executor_dependencies["execution_context"],
    )

    assert result.status == "error"
    assert "delegation_depth_exceeded" in result.content
    delegation_service = executor_dependencies["delegation_service"]
    assert not any(call[0] == "create_pending" for call in delegation_service.calls)


def test_delegation_executor_runs_child_and_marks_completed(executor_dependencies):
    """验证 executor 创建 child turn、派生 profile、运行 child 并标记成功。

    参数:
        executor_dependencies: fake executor 协作者集合。

    返回:
        无。

    异常:
        AssertionError: 当结果或副作用不符合预期时由 pytest 抛出。

    副作用:
        调用 DelegationExecutor.execute。
    """

    executor = DelegationExecutor(**executor_dependencies)
    result = executor.execute(
        DelegateTaskArgs(
            child_agent_id="delegate_reviewer",
            delegation_type="review",
            prompt="review selected files",
            requested_tools=["read_file", "delegate_task"],
        ),
        execution_context=executor_dependencies["execution_context"],
    )

    assert result.status == "success"
    assert result.content == "child done"
    delegation_service = executor_dependencies["delegation_service"]
    assert delegation_service.calls[1][0] == "create_pending"
    assert delegation_service.calls[1][1]["effective_tools"] == ("read_file",)
    assert ("mark_child_started", ("delegation_1", "child_turn_1", None)) in (
        delegation_service.calls
    )
    assert ("mark_completed", ("delegation_1", "child done", None)) in delegation_service.calls
    turn_service = executor_dependencies["turn_service"]
    assert turn_service.calls[0][1]["delegation_id"] == "delegation_1"
    runner = executor_dependencies["child_runner"]
    assert runner.child_profiles[0].turn.turn_id == "child_turn_1"
    assert runner.child_profiles[0].allowed_tools == ["read_file"]
    assert "delegate_task" not in runner.child_profiles[0].allowed_tools
    assert runner.child_profiles[0].context_excluded_turn_ids == ("parent_turn_1",)


def test_delegation_executor_passes_runtime_event_loop_to_service(executor_dependencies):
    """验证 executor 会把工具线程携带的 runtime event loop 传给 delegation service。

    参数:
        executor_dependencies: fake executor 协作者集合。

    返回:
        无。

    异常:
        AssertionError: 当状态事件没有收到同一个 loop 对象时由 pytest 抛出。

    副作用:
        调用 DelegationExecutor.execute。
    """

    runtime_event_loop = Mock()
    execution_context = replace(
        executor_dependencies["execution_context"],
        runtime_dependencies=ToolRuntimeDependencies(runtime_event_loop=runtime_event_loop),
    )
    executor_dependencies["execution_context"] = execution_context
    executor = DelegationExecutor(**executor_dependencies)

    executor.execute(
        DelegateTaskArgs(
            child_agent_id="delegate_reviewer",
            delegation_type="review",
            prompt="review selected files",
            requested_tools=["read_file"],
        ),
        execution_context=execution_context,
    )

    delegation_service = executor_dependencies["delegation_service"]
    assert delegation_service.calls[1][1]["runtime_event_loop"] is runtime_event_loop
    assert ("mark_child_started", ("delegation_1", "child_turn_1", runtime_event_loop)) in (
        delegation_service.calls
    )
    assert (
        "mark_completed",
        ("delegation_1", "child done", runtime_event_loop),
    ) in delegation_service.calls


def test_delegation_service_publishes_via_runtime_event_loop():
    """验证 delegation live event 通过 runtime event loop 线程安全发布。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当 publish 未通过 call_soon_threadsafe 调度时由 pytest 抛出。

    副作用:
        调用 DelegationService.create_pending 并写入 fake service 记录。
    """

    crud = FakeDelegationCrud()
    runtime_event_service = FakeRuntimeEventService()
    runtime_event_loop = Mock()
    service = DelegationService(crud, runtime_event_service)

    delegation_id = service.create_pending(
        task_id="task_1",
        parent_turn_id="parent_turn_1",
        parent_agent_id="developer",
        child_agent_id="delegate_reviewer",
        delegation_type="review",
        prompt="review",
        requested_tools=("read_file",),
        effective_tools=("read_file",),
        runtime_event_loop=runtime_event_loop,
    )

    assert delegation_id in crud.records
    assert len(runtime_event_service.saved) == 1
    assert runtime_event_service.published == []
    runtime_event_loop.call_soon_threadsafe.assert_called_once_with(
        runtime_event_service.publish_event,
        runtime_event_service.saved[0],
    )


def test_agent_runtime_cancel_turn_cascades_to_active_child(monkeypatch):
    """验证父 turn 取消会级联取消 active child turn 与 delegation。

    参数:
        monkeypatch: pytest monkeypatch fixture，用于替换 runtime 的 service 依赖入口。

    返回:
        无。

    异常:
        AssertionError: 当 child turn、取消信号或 delegation 终态不符合预期时抛出。

    副作用:
        标记进程内 cancellation registry；测试结束前清理相关 turn 标识。
    """

    turn_service = FakeCascadeTurnService()
    delegation_service = FakeCascadeDelegationService()
    runtime = object.__new__(AgentRuntime)
    runtime._turn_service = turn_service
    saved_events = []
    stable_turns = []

    def fake_save_and_publish(event):
        """记录 runtime event。

        参数:
            event: 待记录的 runtime event。

        返回:
            原样返回事件。

        异常:
            无。

        副作用:
            写入 saved_events 列表。
        """

        saved_events.append(event)
        return event

    def fake_mark_stable(turn_id: str) -> None:
        """记录稳定文件变更的 turn。

        参数:
            turn_id: turn 标识。

        返回:
            无。

        异常:
            无。

        副作用:
            写入 stable_turns 列表。
        """

        stable_turns.append(turn_id)

    runtime._save_and_publish_runtime_event = fake_save_and_publish
    runtime._mark_stable_file_changes = fake_mark_stable
    monkeypatch.setattr(
        "app.core.runtime.runner.get_delegation_service",
        lambda: delegation_service,
    )

    try:
        turn = runtime.cancel_turn("parent_turn_1")

        assert turn.status == "cancelled"
        assert ("child_turn_1", "parent_turn_cancelled") in turn_service.cancelled_turns
        assert cancellation_registry.is_cancelled("child_turn_1")
        assert ("delegation_1", "parent_turn_cancelled") in delegation_service.cancelled
        assert any(
            event.event_type == EventType.RUN_CANCELLED and event.turn_id == "child_turn_1"
            for event in saved_events
        )
        assert "child_turn_1" in stable_turns
    finally:
        cancellation_registry.clear("parent_turn_1")
        cancellation_registry.clear("child_turn_1")


@pytest.mark.parametrize(
    "runner_result,expected_status,expected_call,expected_error",
    [
        (
            DelegationResult(
                status="failed",
                child_turn_id="child_turn_1",
                error="child failed",
            ),
            "error",
            "mark_failed",
            "child failed",
        ),
        (
            DelegationResult(
                status="cancelled",
                child_turn_id="child_turn_1",
                error="child cancelled",
            ),
            "error",
            "mark_cancelled",
            "child cancelled",
        ),
    ],
)
def test_delegation_executor_returns_tool_error_for_child_terminal_failures(
    executor_dependencies,
    runner_result: DelegationResult,
    expected_status: str,
    expected_call: str,
    expected_error: str,
):
    """验证 child failed/cancelled 会落终态并返回工具错误。

    参数:
        executor_dependencies: fake executor 协作者集合。
        runner_result: fake child runner 返回的终态。
        expected_status: 期望的工具观察状态。
        expected_call: 期望调用的 delegation service 终态方法。
        expected_error: 期望透传的错误摘要。

    返回:
        无。

    异常:
        AssertionError: 当结果或副作用不符合预期时由 pytest 抛出。

    副作用:
        调用 DelegationExecutor.execute。
    """

    executor_dependencies["child_runner"] = FakeChildRunner(runner_result)
    executor = DelegationExecutor(**executor_dependencies)
    result = executor.execute(
        DelegateTaskArgs(
            child_agent_id="delegate_reviewer",
            delegation_type="review",
            prompt="review",
            requested_tools=["read_file"],
        ),
        execution_context=executor_dependencies["execution_context"],
    )

    assert result.status == expected_status
    assert expected_error in result.content
    delegation_service = executor_dependencies["delegation_service"]
    assert (expected_call, ("delegation_1", expected_error, None)) in delegation_service.calls


def test_child_agent_runner_uses_final_response_summary():
    """验证 ChildAgentRunner 从 child runtime 事件中提取最终摘要。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当 runner 未消费 final_response 或 RUN_FINISHED 时由 pytest 抛出。

    副作用:
        通过 asyncio.run 间接消费 fake async generator。
    """

    child_profile = replace(default_developer_agent(), turn=_parent_turn())

    async def fake_run_agent(profile):
        """生成成功 child run 的 fake runtime event 流。

        参数:
            profile: child AgentProfile。

        返回:
            异步生成器逐个产出 RuntimeEvent。

        异常:
            无。

        副作用:
            无。
        """

        yield RuntimeEvent(
            event_type=EventType.FINAL_RESPONSE,
            task_id=profile.turn.task_id,
            turn_id=profile.turn.turn_id,
            payload=FinalResponsePayload(
                text="review summary",
                step_id="step_1",
                status="completed",
            ),
        )
        yield RuntimeEvent(
            event_type=EventType.RUN_FINISHED,
            task_id=profile.turn.task_id,
            turn_id=profile.turn.turn_id,
            payload=RunFinishedPayload(status="completed"),
        )

    result = ChildAgentRunner(fake_run_agent).run_child(child_profile)

    assert result.status == "completed"
    assert result.summary == "review summary"


async def test_child_agent_runner_fails_fast_inside_running_event_loop():
    """验证 ChildAgentRunner 在已有事件循环线程中确定性失败。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当 runner 没有返回固定失败码时由 pytest 抛出。

    副作用:
        在 pytest-asyncio 事件循环中调用 run_child。
    """

    child_profile = replace(default_developer_agent(), turn=_parent_turn())

    async def fake_run_agent(profile):
        """定义不应被调用的 fake runtime event 流。

        参数:
            profile: child AgentProfile。

        返回:
            空异步生成器。

        异常:
            AssertionError: 如果 runner 错误进入该生成器。

        副作用:
            无。
        """

        raise AssertionError("fake_run_agent should not be called from an event loop thread")
        yield

    result = ChildAgentRunner(fake_run_agent).run_child(child_profile)

    assert result.status == "failed"
    assert result.error == "delegation_runner_event_loop_thread"


def test_child_agent_runner_classifies_cancelled_without_terminal_event():
    """验证 child 事件流无终态但取消信号存在时会归类为 cancelled。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当 runner 把取消误归类为 failed 时由 pytest 抛出。

    副作用:
        通过 asyncio.run 间接消费 fake async generator。
    """

    child_profile = replace(default_developer_agent(), turn=_parent_turn())

    async def fake_run_agent(profile):
        """生成不含 RUN_CANCELLED 的 fake runtime event 流。

        参数:
            profile: child AgentProfile。

        返回:
            异步生成器逐个产出 RuntimeEvent。

        异常:
            无。

        副作用:
            无。
        """

        if False:
            yield RuntimeEvent(
                event_type=EventType.FINAL_RESPONSE,
                task_id=profile.turn.task_id,
                turn_id=profile.turn.turn_id,
                payload=FinalResponsePayload(
                    text="never",
                    step_id="step_1",
                    status="completed",
                ),
            )

    result = ChildAgentRunner(
        fake_run_agent,
        should_cancel=lambda turn_id: turn_id == child_profile.turn.turn_id,
    ).run_child(child_profile)

    assert result.status == "cancelled"
    assert result.error == "child turn cancelled"
