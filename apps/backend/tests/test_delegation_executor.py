"""core delegation executor tests."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.config.configuration import build_agent_registry
from app.core.agents.define_agents import default_developer_agent
from app.core.delegation.child_agent_runner import ChildAgentRunner
from app.core.delegation.delegation_executor import DelegationExecutor
from app.core.runtime.runner import AgentRuntime
from app.core.runtime.turn_cancellation_registry import cancellation_registry
from app.models import TaskRecord
from app.models.delegation_record import DelegationRecord
from app.models.enums.event_type import EventType
from app.models.event.runtime_event import RuntimeEvent
from app.models.payload.final_response_payload import FinalResponsePayload
from app.models.payload.run_finished_payload import RunFinishedPayload
from app.models.payload.run_started_payload import RunStartedPayload
from app.models.turn_record import TurnRecord
from app.service.delegation.delegation_acquire_result import DelegationAcquireResult
from app.service.delegation.delegation_result import DelegationResult
from app.service.delegation.delegation_service import DelegationService
from app.tools.schemas import ToolExecutionContext
from app.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies
from app.tools.tool_models.delegate_task_args import DelegateTaskArgs
from app.utils.datetime_utils import utc_now


def _parent_task(parent_task_id: str | None = None) -> TaskRecord:
    """构造测试用发起方 task 记录（与生产 TaskRecord 同构，防止 fake 签名漂移）。

    参数:
        parent_task_id: 可选的父 task 标识；非 None 时模拟「发起方自身是委派
            子 task」的递归委派场景（executor 据此判定 depth=1）。

    返回:
        测试用 TaskRecord。

    异常:
        无。

    副作用:
        无。
    """

    return TaskRecord(
        task_id="task_1",
        workspace_id="workspace_1",
        agent_id="developer",
        title="parent task",
        status="running",
        created_at=utc_now(),
        updated_at=utc_now(),
        parent_task_id=parent_task_id,
    )


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

    def try_create_pending(
        self,
        task_id: str,
        parent_turn_id: str,
        parent_agent_id: str,
        child_agent_id: str,
        delegation_type: str,
        prompt: str,
        effective_tools: tuple[str, ...],
        max_concurrency: int,
        runtime_event_loop=None,
    ) -> DelegationAcquireResult:
        """执行原子 acquire 并返回委派获取结果。

        ``acquired`` 由构造时 ``active_children >= max_concurrency`` 决定；测试可借此
        模拟并发额度已满的拒绝路径。

        参数:
            task_id: 所属任务标识。
            parent_turn_id: 父 turn 标识。
            parent_agent_id: 父 Agent 标识。
            child_agent_id: child Agent 标识。
            delegation_type: 委派类型（由 child profile 派生）。
            prompt: 拼装后的结构化 child 任务文本。
            effective_tools: 策略收敛后的工具集合。
            max_concurrency: 并发上限。

        返回:
            成功时 ``acquired=True`` 的固定 delegation 标识；额度满时 ``acquired=False``。

        异常:
            无。

        副作用:
            记录 try_create_pending 调用及其关键参数。
        """

        if self.active_children >= max_concurrency:
            self.calls.append(
                (
                    "try_create_pending",
                    {
                        "task_id": task_id,
                        "parent_turn_id": parent_turn_id,
                        "parent_agent_id": parent_agent_id,
                        "child_agent_id": child_agent_id,
                        "delegation_type": delegation_type,
                        "prompt": prompt,
                        "effective_tools": effective_tools,
                        "max_concurrency": max_concurrency,
                        "runtime_event_loop": runtime_event_loop,
                    },
                )
            )
            return DelegationAcquireResult(
                acquired=False,
                delegation_id="",
                reason=(
                    "the concurrency limit for this parent turn was reached; "
                    "wait for an active child to finish before delegating again."
                ),
            )
        self.created_delegation_id = "delegation_1"
        self.calls.append(
            (
                "try_create_pending",
                {
                    "task_id": task_id,
                    "parent_turn_id": parent_turn_id,
                    "parent_agent_id": parent_agent_id,
                    "child_agent_id": child_agent_id,
                    "delegation_type": delegation_type,
                    "prompt": prompt,
                    "effective_tools": effective_tools,
                    "max_concurrency": max_concurrency,
                    "runtime_event_loop": runtime_event_loop,
                },
            )
        )
        return DelegationAcquireResult(
            acquired=True,
            delegation_id=self.created_delegation_id,
            reason="",
        )

    def mark_child_started(
        self,
        delegation_id: str,
        child_turn_id: str,
        child_task_id: str | None = None,
        runtime_event_loop=None,
    ) -> None:
        """记录 child turn 已开始。

        参数:
            delegation_id: 委派标识。
            child_turn_id: child turn 标识。
            child_task_id: 可选的 child task 标识（重构后随子任务模型一并落库）。

        返回:
            无。

        异常:
            无。

        副作用:
            记录 mark_child_started 调用。
        """

        self.calls.append(
            (
                "mark_child_started",
                (delegation_id, child_turn_id, child_task_id, runtime_event_loop),
            )
        )

    def mark_completed(
        self,
        delegation_id: str,
        summary: str,
        child_task_id: str | None = None,
        runtime_event_loop=None,
    ) -> None:
        """记录委派成功终态。

        参数:
            delegation_id: 委派标识。
            summary: child 执行摘要。
            child_task_id: 可选的 child task 标识（重构后随子任务模型一并落库）。

        返回:
            无。

        异常:
            无。

        副作用:
            记录 mark_completed 调用。
        """

        self.calls.append(
            ("mark_completed", (delegation_id, summary, child_task_id, runtime_event_loop))
        )

    def mark_failed(
        self,
        delegation_id: str,
        error: str,
        child_task_id: str | None = None,
        runtime_event_loop=None,
    ) -> None:
        """记录委派失败终态。

        参数:
            delegation_id: 委派标识。
            error: 失败原因。
            child_task_id: 可选的 child task 标识（重构后随子任务模型一并落库）。

        返回:
            无。

        异常:
            无。

        副作用:
            记录 mark_failed 调用。
        """

        self.calls.append(
            ("mark_failed", (delegation_id, error, child_task_id, runtime_event_loop))
        )

    def mark_cancelled(
        self,
        delegation_id: str,
        error: str,
        child_task_id: str | None = None,
        runtime_event_loop=None,
    ) -> None:
        """记录委派取消终态。

        参数:
            delegation_id: 委派标识。
            error: 取消原因。
            child_task_id: 可选的 child task 标识（重构后随子任务模型一并落库）。

        返回:
            无。

        异常:
            无。

        副作用:
            记录 mark_cancelled 调用。
        """

        self.calls.append(
            ("mark_cancelled", (delegation_id, error, child_task_id, runtime_event_loop))
        )


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

    def create_turn(
        self,
        task_id: str,
        input_text: str,
        agent_id: str,
    ) -> TurnRecord:
        """创建测试用 child turn 记录（与生产 TurnService.create_turn 签名对齐）。

        参数:
            task_id: 所属任务标识。
            input_text: child 输入文本。
            agent_id: child agent 标识。

        返回:
            pending 状态的 TurnRecord。

        异常:
            无。

        副作用:
            记录 create_turn 调用。
        """

        self.calls.append(
            (
                "create_turn",
                {
                    "task_id": task_id,
                    "input_text": input_text,
                    "agent_id": agent_id,
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


class FakeTaskService:
    """记录 executor 的 create_child_task 调用的 fake task service（不接触真实存储）。"""

    def __init__(self) -> None:
        """初始化调用记录与返回的 child task 标识。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            初始化调用列表与计数器。
        """

        self.calls: list[tuple[str, object]] = []
        self._child_counter = 0

    def create_child_task(
        self,
        *,
        title: str,
        parent_task_id: str,
        parent_turn_id: str,
        delegation_id: str,
        workspace_id: str,
        agent_id: str,
    ) -> TaskRecord:
        """返回预设的 delegation 类型子任务记录并记录调用（与生产签名对齐）。

        参数:
            title: 子任务标题。
            parent_task_id: 父任务标识。
            parent_turn_id: 父 turn 标识。
            delegation_id: 委派标识。
            workspace_id: 工作区标识。
            agent_id: 子 agent 标识。

        返回:
            带父子关联字段的 ``TaskRecord``（``task_type='delegation'``）。

        异常:
            无。

        副作用:
            记录 create_child_task 调用并自增 child 计数。
        """

        self._child_counter += 1
        child_task_id = f"child_task_{self._child_counter}"
        self.calls.append(
            (
                "create_child_task",
                {
                    "title": title,
                    "parent_task_id": parent_task_id,
                    "parent_turn_id": parent_turn_id,
                    "delegation_id": delegation_id,
                    "workspace_id": workspace_id,
                    "agent_id": agent_id,
                },
            )
        )
        return TaskRecord(
            task_id=child_task_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            title=title,
            status="pending",
            created_at=utc_now(),
            updated_at=utc_now(),
            task_type="delegation",
            parent_task_id=parent_task_id,
            parent_turn_id=parent_turn_id,
            delegation_id=delegation_id,
        )


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

    def run_child(self, child_profile, delegation_id=""):
        """返回预设 child 执行结果。

        参数:
            child_profile: executor 派生出的本次 child AgentProfile。
            delegation_id: 透传的委派标识，与生产 ChildAgentRunner.run_child 对齐。

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
            effective_tools=("read_file",),
            created_at=now,
            updated_at=now,
            child_task_id="",
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


def _parent_turn() -> TurnRecord:
    """构造测试用 parent turn。

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
def executor_dependencies(tmp_path: Path, monkeypatch) -> dict[str, object]:
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
    agent_registry = build_agent_registry()
    delegation_service = FakeDelegationService()
    turn_service = FakeTurnService()
    task_service = FakeTaskService()
    monkeypatch.setattr(
        "app.core.delegation.delegation_executor.get_agent_registry",
        lambda: agent_registry,
    )
    monkeypatch.setattr(
        "app.core.delegation.delegation_executor.get_delegation_service",
        lambda: delegation_service,
    )
    monkeypatch.setattr(
        "app.core.delegation.delegation_executor.get_turn_service",
        lambda: turn_service,
    )
    # get_task_service 由 executor 在 execute 内通过 ``from app.service.depends
    # import get_task_service`` 局部导入，因此必须替换 service 依赖入口本身。
    monkeypatch.setattr(
        "app.service.depends.get_task_service",
        lambda: task_service,
    )
    return {
        "delegation_service": delegation_service,
        "turn_service": turn_service,
        "task_service": task_service,
        "child_runner": FakeChildRunner(
            DelegationResult(
                status="completed",
                child_turn_id="child_turn_1",
                summary="child done",
            )
        ),
        "parent_profile": parent_profile,
        "parent_turn": _parent_turn(),
        "parent_task": _parent_task(),
        "execution_context": _execution_context(tmp_path),
    }


def _executor_kwargs(executor_dependencies: dict[str, object]) -> dict[str, object]:
    """提取 DelegationExecutor 构造所需的 turn 级依赖。
    参数:
        executor_dependencies: fake executor 协作者集合。
    返回:
        可直接展开传入 DelegationExecutor 的构造参数。
    异常:
        KeyError: 当 fixture 缺少必要键时由 dict 访问抛出。
    副作用:
        无。
    """

    return {
        "child_runner": executor_dependencies["child_runner"],
        "parent_profile": executor_dependencies["parent_profile"],
        "parent_turn": executor_dependencies["parent_turn"],
        "parent_task": executor_dependencies["parent_task"],
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

    executor = DelegationExecutor(**_executor_kwargs(executor_dependencies))
    result = executor.execute(
        DelegateTaskArgs(
            child_agent_id="missing_agent",
            title="Review files",
            objective="review selected files",
            rules=[],
            references=[],
            expected_output="review comments",
        ),
        execution_context=executor_dependencies["execution_context"],
    )

    assert result.status == "error"
    assert "child not found" in result.content
    delegation_service = executor_dependencies["delegation_service"]
    assert not any(call[0] == "try_create_pending" for call in delegation_service.calls)


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

    # 发起方 task 自身是委派子 task（parent_task_id 非空）→ depth=1 >= max_depth=1 → 策略拒绝
    executor_dependencies["parent_task"] = _parent_task(parent_task_id="grand_parent_task")
    executor = DelegationExecutor(**_executor_kwargs(executor_dependencies))
    result = executor.execute(
        DelegateTaskArgs(
            child_agent_id="delegate_reviewer",
            title="Review files",
            objective="review selected files",
            rules=[],
            references=[],
            expected_output="review comments",
        ),
        execution_context=executor_dependencies["execution_context"],
    )

    assert result.status == "error"
    assert "delegation_depth_exceeded" in result.content
    delegation_service = executor_dependencies["delegation_service"]
    assert not any(call[0] == "try_create_pending" for call in delegation_service.calls)


def test_execute_rejects_when_concurrency_limit_reached(executor_dependencies):
    """验证并发额度已满时 executor 不创建 child turn，返回错误 observation。

    委派并发额度由 storage 层原子 acquire 裁决；本用例通过 Fake service 的
    ``active_children >= max_concurrency`` 模拟「额度已满」，断言 executor 返回
    error observation、不创建 child turn，且 try_create_pending 被调用。

    参数:
        executor_dependencies: fake executor 协作者集合（此处仅借用 execution_context 等）。

    返回:
        无。

    异常:
        AssertionError: 当结果或副作用不符合预期时由 pytest 抛出。

    副作用:
        调用 DelegationExecutor.execute。
    """

    deps = dict(executor_dependencies)
    # executor 通过 get_delegation_service() 单例取 service（fixture 已 monkeypatch 指向
    # executor_dependencies["delegation_service"] 实例），因此必须直接调整该实例的
    # active_children 来模拟「额度已满」，而不能替换 deps 中未被使用的引用。
    # 默认 Settings.DELEGATION_MAX_CONCURRENCY == 2，active_children=2 即触发拒绝路径。
    delegation_service = deps["delegation_service"]
    delegation_service.active_children = 2
    executor = DelegationExecutor(**_executor_kwargs(deps))
    result = executor.execute(
        DelegateTaskArgs(
            child_agent_id="delegate_reviewer",
            title="Review diff",
            objective="review selected files",
            rules=["do not modify files"],
            references=["app/core/runtime/runner.py"],
            expected_output="a list of review comments",
        ),
        execution_context=deps["execution_context"],
    )

    assert result.status == "error"
    assert "concurrency" in result.content or "concurrency" in (result.reason or "")
    assert any(call[0] == "try_create_pending" for call in delegation_service.calls)
    # 额度已满时不应进入 child turn 创建
    assert not any(call[0] == "create_turn" for call in deps["turn_service"].calls)


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

    executor = DelegationExecutor(**_executor_kwargs(executor_dependencies))
    result = executor.execute(
        DelegateTaskArgs(
            child_agent_id="delegate_reviewer",
            title="Review diff",
            objective="review selected files",
            rules=["do not modify files", "do not run tests"],
            references=["app/core/runtime/runner.py"],
            expected_output="a list of review comments",
        ),
        execution_context=executor_dependencies["execution_context"],
    )

    assert result.status == "success"
    assert result.content == "child done"
    delegation_service = executor_dependencies["delegation_service"]
    # try_create_pending 是 executor 对 delegation service 的首个调用（calls[0]），
    # 此处不依赖硬编码索引，改为搜索匹配调用条目。
    pending_call = next(
        call for call in delegation_service.calls if call[0] == "try_create_pending"
    )
    assert pending_call[0] == "try_create_pending"
    create_call = pending_call[1]
    # 有效工具为 child profile 自身权限剔除 delegate_task（避免递归委派），
    # 不再与父/系统权限做交集，故基于真实 child profile 推导期望值，避免硬编码脆弱。
    expected_child = build_agent_registry().resolve("delegate_reviewer")
    expected_tools = tuple(sorted(set(expected_child.allowed_tools) - {"delegate_task"}))
    assert create_call["effective_tools"] == expected_tools
    # delegation_type 来自 child profile（delegate_reviewer → reviewer），非模型入参
    assert create_call["delegation_type"] == "reviewer"
    # 拼装后的结构化 objective 文本应包含各段内容与英文标签
    prompt_text = create_call["prompt"]
    assert "# Review diff" in prompt_text
    assert "## Objective" in prompt_text
    assert "review selected files" in prompt_text
    assert "## Rules" in prompt_text
    assert "- do not modify files" in prompt_text
    assert "- do not run tests" in prompt_text
    assert "## References" in prompt_text
    assert "- app/core/runtime/runner.py" in prompt_text
    assert "## Expected Output" in prompt_text
    assert "a list of review comments" in prompt_text
    # 第三个位置参数是 executor 透传给 delegation service 的 runtime_event_loop，
    # 它来自 execution_context 的 runtime_dependencies，非 None。
    runtime_event_loop = executor_dependencies[
        "execution_context"
    ].runtime_dependencies.runtime_event_loop
    assert (
        "mark_child_started",
        ("delegation_1", "child_turn_1", "child_task_1", runtime_event_loop),
    ) in delegation_service.calls
    assert (
        "mark_completed",
        ("delegation_1", "child done", "child_task_1", runtime_event_loop),
    ) in delegation_service.calls
    # 重构核心：executor 在 child turn 之前先经 TaskService 创建独立的 delegation 子任务，
    # 且子任务挂在 child_task_1 下（task_id 与父 task 解耦），验证父子 task 模型落地。
    task_service = executor_dependencies["task_service"]
    assert any(call[0] == "create_child_task" for call in task_service.calls)
    child_create = next(call for call in task_service.calls if call[0] == "create_child_task")
    assert child_create[1]["parent_task_id"] == "task_1"
    assert child_create[1]["delegation_id"] == "delegation_1"
    assert child_create[1]["agent_id"] == "delegate_reviewer"
    # child turn 必须挂在子任务（child_task_1）下，而非父 task，以隔离上下文。
    turn_service = executor_dependencies["turn_service"]
    assert turn_service.calls[0][0] == "create_turn"
    assert turn_service.calls[0][1]["task_id"] == "child_task_1"
    # child turn 的 input_text 使用同一份拼装文本
    assert turn_service.calls[0][1]["input_text"] == prompt_text
    runner = executor_dependencies["child_runner"]
    assert runner.child_profiles[0].turn.turn_id == "child_turn_1"
    # child 的 allowed_tools 由策略层收窄为 child profile 自身权限剔除 delegate_task，
    # 而非旧的三方交集（read_file）。用同一期望值保证 executor 透传一致。
    assert runner.child_profiles[0].allowed_tools == list(expected_tools)
    assert "delegate_task" not in runner.child_profiles[0].allowed_tools


def test_delegation_executor_includes_background_section_in_child_input(
    executor_dependencies,
):
    """验证可选的 background 字段会被拼装为 child 输入文本的 Background section。

    参数:
        executor_dependencies: fake executor 协作者集合。

    返回:
        无。

    异常:
        AssertionError: 当 background 未出现在子输入文本或 section 标签缺失时由 pytest 抛出。

    副作用:
        调用 DelegationExecutor.execute。
    """

    executor = DelegationExecutor(**_executor_kwargs(executor_dependencies))

    executor.execute(
        DelegateTaskArgs(
            child_agent_id="delegate_reviewer",
            title="Review diff",
            objective="review selected files",
            rules=["do not modify files", "do not run tests"],
            references=["app/core/runtime/runner.py"],
            expected_output="a list of review comments",
            background="assume the branch is already rebased onto main",
        ),
        execution_context=executor_dependencies["execution_context"],
    )

    delegation_service = executor_dependencies["delegation_service"]
    prompt_text = next(
        call for call in delegation_service.calls if call[0] == "try_create_pending"
    )[1]["prompt"]
    assert "## Background" in prompt_text
    assert "assume the branch is already rebased onto main" in prompt_text
    assert executor_dependencies["turn_service"].calls[0][1]["input_text"] == prompt_text


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
    executor = DelegationExecutor(**_executor_kwargs(executor_dependencies))

    executor.execute(
        DelegateTaskArgs(
            child_agent_id="delegate_reviewer",
            title="Review files",
            objective="review selected files",
            rules=[],
            references=[],
            expected_output="review comments",
        ),
        execution_context=execution_context,
    )

    delegation_service = executor_dependencies["delegation_service"]
    # 从 calls 中搜索 try_create_pending 条目取 runtime_event_loop，不依赖硬编码索引。
    pending_call = next(
        call for call in delegation_service.calls if call[0] == "try_create_pending"
    )
    assert pending_call[1]["runtime_event_loop"] is runtime_event_loop
    # mark_child_started 调用元组为 (delegation_id, child_turn_id, child_task_id, loop)。
    assert (
        "mark_child_started",
        ("delegation_1", "child_turn_1", "child_task_1", runtime_event_loop),
    ) in delegation_service.calls
    assert (
        "mark_completed",
        ("delegation_1", "child done", "child_task_1", runtime_event_loop),
    ) in delegation_service.calls
    runner = executor_dependencies["child_runner"]
    assert runner.child_profiles[0].runtime_event_loop is runtime_event_loop


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


def test_cancel_turn_does_not_emit_parent_run_cancelled(monkeypatch):
    """验证 ``cancel_turn`` 不再为 parent turn 自身广播空壳 ``RUN_CANCELLED``。

    取消终态事件收口到实际检测到取消的执行节点（model_node / tools_node），
    由 ``cancel_turn`` 额外广播会导致同一 turn 出现两张「任务已取消」。

    参数:
        monkeypatch: pytest monkeypatch fixture。

    返回:
        无。

    异常:
        AssertionError: 当 parent turn 收到 ``RUN_CANCELLED`` 时抛出。
    """

    turn_service = FakeCascadeTurnService()
    delegation_service = FakeCascadeDelegationService()
    runtime = object.__new__(AgentRuntime)
    runtime._turn_service = turn_service
    saved_events = []

    def fake_save_and_publish(event):
        """记录 runtime event。"""
        saved_events.append(event)
        return event

    runtime._save_and_publish_runtime_event = fake_save_and_publish
    runtime._mark_stable_file_changes = lambda _: None
    monkeypatch.setattr(
        "app.core.runtime.runner.get_delegation_service",
        lambda: delegation_service,
    )

    try:
        runtime.cancel_turn("parent_turn_1")
        parent_cancelled = [
            event
            for event in saved_events
            if event.event_type == EventType.RUN_CANCELLED and event.turn_id == "parent_turn_1"
        ]
        assert parent_cancelled == []
        # child 取消事件仍应正常发出（属于子任务独立终态）
        assert any(
            event.event_type == EventType.RUN_CANCELLED and event.turn_id == "child_turn_1"
            for event in saved_events
        )
    finally:
        cancellation_registry.clear("parent_turn_1")
        cancellation_registry.clear("child_turn_1")


async def test_agent_runtime_emit_schedules_child_event_publish_on_profile_loop():
    """验证 child runtime 事件会调度回父运行事件循环发布。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当 _emit 直接 publish 或未调度到指定 loop 时由 pytest 抛出。

    副作用:
        调用 AgentRuntime._emit 并写入 fake runtime event service 记录。
    """

    runtime = object.__new__(AgentRuntime)
    runtime_event_service = FakeRuntimeEventService()
    runtime._runtime_event_service = runtime_event_service
    runtime_event_loop = Mock()
    child_profile = replace(
        default_developer_agent(),
        runtime_event_loop=runtime_event_loop,
    )
    event = RuntimeEvent(
        event_type=EventType.RUN_STARTED,
        task_id="task_1",
        turn_id="child_turn_1",
        payload=RunStartedPayload(status="running", agent_id="delegate_reviewer"),
    )

    stamped = await runtime._emit(event, child_profile)

    assert stamped is event
    assert runtime_event_service.saved == [event]
    assert runtime_event_service.published == []
    runtime_event_loop.call_soon_threadsafe.assert_called_once_with(
        runtime_event_service.publish_event,
        event,
    )


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
            "cancelled",
            "mark_cancelled",
            "child cancelled",
        ),
    ],
)
def test_delegation_executor_maps_child_terminal_failures_to_tool_status(
    executor_dependencies,
    runner_result: DelegationResult,
    expected_status: str,
    expected_call: str,
    expected_error: str,
):
    """验证 child failed/cancelled 会落终态并映射为对应的工具观察状态。

    failed 映射为 ``status="error"``（真实执行故障）；cancelled 映射为 ``status="cancelled"``
    （用户主动中断的确定性终态，与失败语义不同，不应塌成 error 导致前端误读为执行失败）。

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
    executor = DelegationExecutor(**_executor_kwargs(executor_dependencies))
    result = executor.execute(
        DelegateTaskArgs(
            child_agent_id="delegate_reviewer",
            title="Review",
            objective="review",
            rules=[],
            references=[],
            expected_output="review comments",
        ),
        execution_context=executor_dependencies["execution_context"],
    )

    assert result.status == expected_status
    assert expected_error in result.content
    delegation_service = executor_dependencies["delegation_service"]
    assert (
        expected_call,
        ("delegation_1", expected_error, "child_task_1", None),
    ) in delegation_service.calls


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


def test_child_agent_runner_drains_runtime_stream_after_terminal_event():
    """验证 ChildAgentRunner 收到终态后仍会消费完 runtime 事件流。
    参数:
        无。
    返回:
        无。
    异常:
        AssertionError: 当 runner 在终态事件处提前关闭 runtime generator 时由 pytest 抛出。
    副作用:
        通过 fake async generator 设置 consumed_after_terminal 标记。
    """

    child_profile = replace(default_developer_agent(), turn=_parent_turn())
    consumed_after_terminal = False

    async def fake_run_agent(profile):
        """生成终态后仍有后置收尾步骤的 fake runtime event 流。
        参数:
            profile: child AgentProfile。
        返回:
            异步生成器逐个产出 RuntimeEvent。
        异常:
            无。
        副作用:
            RUN_FINISHED 被消费后设置 consumed_after_terminal 标记。
        """

        nonlocal consumed_after_terminal
        yield RuntimeEvent(
            event_type=EventType.RUN_FINISHED,
            task_id=profile.turn.task_id,
            turn_id=profile.turn.turn_id,
            payload=RunFinishedPayload(status="completed"),
        )
        consumed_after_terminal = True

    result = ChildAgentRunner(fake_run_agent).run_child(child_profile)

    assert result.status == "completed"
    assert consumed_after_terminal is True


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


def test_child_agent_runner_prefers_cancel_over_completed_after_terminal():
    """验证取消信号晚于终态事件到达时仍归类为 cancelled。

    父 turn 取消可能恰好在 child 产出 RUN_FINISHED 之后、流耗尽之前到达。
    ``_consume_child_events`` 必须在循环结束后再次检查取消信号（防御性早退分支），
    优先于已设置的 completed 终态，避免把被取消的 child 误报为成功完成。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当取消信号未被尊重时由 pytest 抛出。

    副作用:
        通过 asyncio.run 间接消费 fake async generator。
    """

    child_profile = replace(default_developer_agent(), turn=_parent_turn())
    cancel_after_first_event = {"fired": False}

    def should_cancel(_turn_id: str) -> bool:
        """取消信号在首次事件被消费后置位。

        参数:
            _turn_id: 被查询的 turn 标识（本用例忽略）。

        返回:
            首次事件消费后置 True，否则 False。

        异常:
            无。

        副作用:
            通过闭包翻转 fired 标记。
        """

        return cancel_after_first_event["fired"]

    async def fake_run_agent(profile):
        """先产出 RUN_FINISHED，再产一个会被忽略的 FINAL_RESPONSE 推动循环。

        参数:
            profile: child AgentProfile。

        返回:
            异步生成器逐个产出 RuntimeEvent。

        异常:
            无。

        副作用:
            消费 RUN_FINISHED 后翻转 cancel_after_first_event 闭包标记。
        """

        yield RuntimeEvent(
            event_type=EventType.RUN_FINISHED,
            task_id=profile.turn.task_id,
            turn_id=profile.turn.turn_id,
            payload=RunFinishedPayload(status="completed"),
        )
        cancel_after_first_event["fired"] = True
        yield RuntimeEvent(
            event_type=EventType.FINAL_RESPONSE,
            task_id=profile.turn.task_id,
            turn_id=profile.turn.turn_id,
            payload=FinalResponsePayload(text="late", step_id="step_1", status="completed"),
        )

    result = ChildAgentRunner(fake_run_agent, should_cancel=should_cancel).run_child(child_profile)

    assert result.status == "cancelled"
    assert result.error == "child turn cancelled"


def test_child_agent_runner_fails_without_terminal_event():
    """验证 child 事件流无任何 RUN_* 终态时归类为 failed。

    若 child runtime 只产出过程事件（如 FINAL_RESPONSE）却未产出 RUN_FINISHED /
    RUN_FAILED / RUN_CANCELLED，``_consume_child_events`` 不应把缺失终态误判为完成，
    而应归类为 failed 并携带可排查的 ``ended without terminal event`` 原因。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当缺失终态被误判为非 failed 时由 pytest 抛出。

    副作用:
        通过 asyncio.run 间接消费 fake async generator。
    """

    child_profile = replace(default_developer_agent(), turn=_parent_turn())

    async def fake_run_agent(profile):
        """只产出 FINAL_RESPONSE、不产出任何 RUN_* 终态的 fake 事件流。

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
            payload=FinalResponsePayload(text="orphan", step_id="step_1", status="completed"),
        )

    result = ChildAgentRunner(fake_run_agent).run_child(child_profile)

    assert result.status == "failed"
    assert result.error == "child turn ended without terminal event"


def test_child_agent_runner_runs_on_main_thread_without_running_loop():
    """验证 ChildAgentRunner 在主线程（无运行中事件循环）能正常驱动 child 成功。

    该测试覆盖事件循环探测的回归面：``_is_running_event_loop_thread`` 必须在不持有
    运行中 loop 的线程上返回 ``False``，而非抛出 ``RuntimeError`` 破坏主流程。本测试
    故意置于非 async 上下文（普通函数），模拟正常委派入口的调用线程。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当 runner 在探测处崩溃或未能正常驱动 child 时由 pytest 抛出。

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
                text="main thread ok", step_id="step_1", status="completed"
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
    assert result.summary == "main thread ok"


# ---------------------------------------------------------------------------
# 级联取消健壮性：多 child 并发取消 / 单 child 失败隔离 / 扫描失败容错
# 既有 test_agent_runtime_cancel_turn_cascades_to_active_child 只覆盖「单 child」
# 主路径；下方三个测试补全 _cancel_active_child_turns / _cancel_child_delegation
# 的 for 循环与两个 try/except 分支。
# ---------------------------------------------------------------------------


class _MultiChildTurnService:
    """支持多 child turn 的 fake turn service，供级联取消多分支测试。"""

    def __init__(self) -> None:
        """初始化 parent 与两个 running child turn。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            创建内存 turn 字典与取消记录列表。
        """
        self.parent_turn = _parent_turn()
        self.child_turns = {
            "child_turn_1": replace(self.parent_turn, turn_id="child_turn_1", status="running"),
            "child_turn_2": replace(self.parent_turn, turn_id="child_turn_2", status="running"),
        }
        self.cancelled_turns: list[tuple[str, str]] = []

    def get_turn(self, turn_id: str) -> TurnRecord:
        """返回 parent 或指定 child turn。

        参数:
            turn_id: turn 标识。

        返回:
            匹配的 TurnRecord；不存在时抛出 KeyError。

        异常:
            KeyError: 当 turn_id 不在已知集合中时。

        副作用:
            无。
        """
        if turn_id == self.parent_turn.turn_id:
            return self.parent_turn
        return self.child_turns[turn_id]

    def cancel_turn_if_active(self, turn_id: str, end_reason: str) -> TurnRecord | None:
        """取消 active turn 并记录调用。

        参数:
            turn_id: turn 标识。
            end_reason: 取消原因。

        返回:
            更新后的 turn；已非 active 时返回 None。

        异常:
            无。

        副作用:
            写入 cancelled_turns 并将对应 turn 标记为 cancelled。
        """
        turn = self.get_turn(turn_id)
        if turn.status not in {"pending", "running"}:
            return None
        cancelled = replace(turn, status="cancelled", end_reason=end_reason)
        if turn_id == self.parent_turn.turn_id:
            self.parent_turn = cancelled
        else:
            self.child_turns[turn_id] = cancelled
        self.cancelled_turns.append((turn_id, end_reason))
        return cancelled


class _MultiChildDelegationService:
    """返回多条 running delegation 的 fake delegation service。"""

    def __init__(self, child_turn_ids: tuple[str, ...]) -> None:
        """初始化多条 running delegation 记录。

        参数:
            child_turn_ids: 需要被返回的 child turn 标识集合。

        返回:
            无。

        异常:
            无。

        副作用:
            初始化内存 delegation 记录列表。
        """
        now = utc_now()
        self.records = [
            DelegationRecord(
                delegation_id=f"delegation_{i}",
                task_id="task_1",
                parent_turn_id="parent_turn_1",
                child_turn_id=turn_id,
                parent_agent_id="developer",
                child_agent_id="delegate_reviewer",
                delegation_type="review",
                status="running",
                prompt="review",
                summary="",
                error="",
                effective_tools=("read_file",),
                created_at=now,
                updated_at=now,
                child_task_id="",
            )
            for i, turn_id in enumerate(child_turn_ids, start=1)
        ]
        self.cancelled: list[tuple[str, str]] = []

    def list_active_by_parent_turn(self, parent_turn_id: str):
        """返回 parent 下所有 running delegation。

        参数:
            parent_turn_id: parent turn 标识。

        返回:
            running delegation 列表；不匹配时返回空列表。

        异常:
            无。

        副作用:
            无。
        """
        if parent_turn_id != "parent_turn_1":
            return []
        return [r for r in self.records if r.status == "running"]

    def mark_cancelled(self, delegation_id: str, error: str) -> None:
        """记录 delegation cancelled 终态。

        参数:
            delegation_id: delegation 标识。
            error: 取消原因。

        返回:
            无。

        异常:
            RuntimeError: 测试可注入以模拟写终态失败。

        副作用:
            更新内存记录并写入 cancelled 列表。
        """
        if getattr(self, "_boom_on", None) == delegation_id:
            raise RuntimeError("simulated delegation mark_cancelled failure")
        for idx, record in enumerate(self.records):
            if record.delegation_id == delegation_id:
                self.records[idx] = replace(record, status="cancelled", error=error)
        self.cancelled.append((delegation_id, error))


def _cascade_runtime(
    monkeypatch,
    turn_service,
    delegation_service,
) -> AgentRuntime:
    """组装仅用于级联取消测试的 AgentRuntime 最小骨架。

    参数:
        monkeypatch: pytest monkeypatch fixture，替换模块级 service 入口。
        turn_service: 注入的私有 turn service。
        delegation_service: 注入的私有 delegation service（经模块级入口替换）。

    返回:
        已替换协作者的 AgentRuntime 实例（不触发全局 service 初始化）。

    异常:
        无。

    副作用:
        替换 ``app.core.runtime.runner.get_delegation_service`` 指向 fake；
        屏蔽运行时事件持久化与文件稳定标记副作用。
    """
    runtime = object.__new__(AgentRuntime)
    runtime._turn_service = turn_service
    runtime._save_and_publish_runtime_event = lambda event: event
    runtime._mark_stable_file_changes = lambda _turn_id: None
    monkeypatch.setattr(
        "app.core.runtime.runner.get_delegation_service",
        lambda: delegation_service,
    )
    return runtime


def test_cancel_active_child_turns_cascades_to_multiple_children(monkeypatch):
    """验证父 turn 取消会级联取消所有在跑 child turn，而非仅首个。

    参数:
        monkeypatch: pytest monkeypatch fixture。

    返回:
        无。

    异常:
        AssertionError: 当存在 child 未被取消或取消信号缺失时由 pytest 抛出。

    副作用:
        标记进程内 cancellation registry；测试结束前清理相关 turn 标识。
    """
    turn_service = _MultiChildTurnService()
    delegation_service = _MultiChildDelegationService(
        child_turn_ids=("child_turn_1", "child_turn_2")
    )
    runtime = _cascade_runtime(monkeypatch, turn_service, delegation_service)

    try:
        turn = runtime.cancel_turn("parent_turn_1")

        assert turn.status == "cancelled"
        assert ("child_turn_1", "parent_turn_cancelled") in turn_service.cancelled_turns
        assert ("child_turn_2", "parent_turn_cancelled") in turn_service.cancelled_turns
        assert cancellation_registry.is_cancelled("child_turn_1")
        assert cancellation_registry.is_cancelled("child_turn_2")
        assert ("delegation_1", "parent_turn_cancelled") in delegation_service.cancelled
        assert ("delegation_2", "parent_turn_cancelled") in delegation_service.cancelled
    finally:
        cancellation_registry.clear("parent_turn_1")
        cancellation_registry.clear("child_turn_1")
        cancellation_registry.clear("child_turn_2")


def test_cancel_child_delegation_isolates_single_failure(monkeypatch, caplog):
    """验证单个 child delegation 写终态失败时，其余 child 仍被取消。

    ``_cancel_child_delegation`` 的 try/except 必须吞掉单 child 异常并继续
    处理循环中的其他 child，否则一个坏 child 会阻断整批级联取消。

    参数:
        monkeypatch: pytest monkeypatch fixture。
        caplog: pytest 日志捕获 fixture。

    返回:
        无。

    异常:
        AssertionError: 当健康 child 未被取消或未记录失败日志时由 pytest 抛出。

    副作用:
        标记进程内 cancellation registry；测试结束前清理相关 turn 标识。
    """
    turn_service = _MultiChildTurnService()
    delegation_service = _MultiChildDelegationService(
        child_turn_ids=("child_turn_1", "child_turn_2")
    )
    # 让 delegation_1 写终态失败，验证 delegation_2 不受影响
    delegation_service._boom_on = "delegation_1"
    runtime = _cascade_runtime(monkeypatch, turn_service, delegation_service)

    try:
        runtime.cancel_turn("parent_turn_1")

        # 失败 child 的 turn 取消信号与 cancellation registry 仍应被标记
        assert cancellation_registry.is_cancelled("child_turn_1")
        assert cancellation_registry.is_cancelled("child_turn_2")
        # 健康 child 的 delegation 终态正常写入
        assert ("delegation_2", "parent_turn_cancelled") in delegation_service.cancelled
        # 失败 child 的 delegation 终态未写入，但错误被隔离记录
        assert ("delegation_1", "parent_turn_cancelled") not in delegation_service.cancelled
        assert any(record.message == "delegation_child_cancel_failed" for record in caplog.records)
    finally:
        cancellation_registry.clear("parent_turn_1")
        cancellation_registry.clear("child_turn_1")
        cancellation_registry.clear("child_turn_2")


def test_cancel_active_child_turns_tolerates_scan_failure(monkeypatch, caplog):
    """验证扫描活动 child delegation 抛异常时，父 turn 取消流程仍正常完成。

    ``_cancel_active_child_turns`` 的 try/except 捕获扫描异常后只记日志并 return，
    不应让父 turn 的取消终态失败（否则用户无法取消已派生子任务的父 turn）。

    参数:
        monkeypatch: pytest monkeypatch fixture。
        caplog: pytest 日志捕获 fixture。

    返回:
        无。

    异常:
        AssertionError: 当父 turn 未被取消或未记录扫描失败日志时由 pytest 抛出。

    副作用:
        标记进程内 cancellation registry；测试结束前清理相关 turn标识。
    """
    turn_service = _MultiChildTurnService()

    class _BoomingDelegationService:
        """list_active_by_parent_turn 始终抛异常的 fake service。"""

        def list_active_by_parent_turn(self, parent_turn_id: str):
            """抛出扫描异常以模拟 storage 层故障。

            参数:
                parent_turn_id: parent turn 标识。

            返回:
                永不返回。

            异常:
                RuntimeError: 总是抛出，模拟扫描失败。

            副作用:
                无。
            """
            raise RuntimeError("simulated delegation scan failure")

        def mark_cancelled(self, delegation_id: str, error: str) -> None:
            """扫描失败时不会被调用的占位。

            参数:
                delegation_id: delegation 标识。
                error: 取消原因。

            返回:
                无。

            异常:
                无。

            副作用:
                无。
            """
            pytest.fail("mark_cancelled 不应在扫描失败后调用")

    runtime = _cascade_runtime(monkeypatch, turn_service, _BoomingDelegationService())

    try:
        turn = runtime.cancel_turn("parent_turn_1")

        # 父 turn 取消终态不受影响
        assert turn.status == "cancelled"
        assert any(
            record.message == "delegation_child_cancel_scan_failed" for record in caplog.records
        )
    finally:
        cancellation_registry.clear("parent_turn_1")
