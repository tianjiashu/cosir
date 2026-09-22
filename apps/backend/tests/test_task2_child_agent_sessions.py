"""RED tests for the Task 2 Child Agent session lifecycle."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from app.assistant_transport.service.conversation_run_executor import ConversationRunExecutor
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.workflows.react.state import ReactGraphState
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.service.child_agent.child_agent_recovery import ChildAgentRecoveryHook
from app.service.child_agent.child_agent_run_finalization import (
    ChildAgentRunFinalizationObserver,
)
from app.service.child_agent.child_agent_session_service import (
    ChildAgentSessionError,
    ChildAgentSessionService,
)


@dataclass
class _Task:
    id: int
    workspace_id: int
    title: str
    task_type: str = "delegation"
    parent_task_id: int | None = None
    parent_run_id: int | None = None


@dataclass
class _Run:
    id: int
    task_id: int
    status: str = ConversationRunStatus.PENDING.value
    input_text: str = ""
    agent_id: str | None = "child"
    provider_id: int | None = None
    model_name: str | None = None
    reasoning_effort: str | None = None
    final_output: str | None = None
    end_reason: str | None = None


class _Tasks:
    def __init__(self) -> None:
        self.tasks: dict[int, _Task] = {}
        self.run_records: dict[int, _Run] = {}
        self.next_id = 100
        self.block_get = False
        self.get_entered = threading.Event()
        self.get_release = threading.Event()

    def get_or_create_task(self, **kwargs: Any) -> _Task:
        task = _Task(
            id=self.next_id,
            workspace_id=kwargs["workspace_id"],
            title=kwargs["title"],
            task_type=kwargs.get("task_type", "delegation"),
            parent_task_id=kwargs.get("parent_task_id"),
            parent_run_id=kwargs.get("parent_run_id"),
        )
        self.next_id += 1
        self.tasks[task.id] = task
        return task

    def get_task(self, task_id: int) -> _Task:
        if self.block_get:
            self.get_entered.set()
            self.get_release.wait()
        return self.tasks[task_id]

    def delete_task(self, task_id: int) -> None:
        self.tasks.pop(task_id)

    def list_child_tasks(self, parent_task_id: int, parent_run_id: int) -> list[_Task]:
        return [
            task
            for task in self.tasks.values()
            if task.parent_task_id == parent_task_id and task.parent_run_id == parent_run_id
        ]

    def list_runs_for_task(self, task_id: int) -> list[_Run]:
        return [run for run in self.run_records.values() if run.task_id == task_id]


class _Runs:
    def __init__(self) -> None:
        self.runs: dict[int, _Run] = {}
        self.next_id = 200
        self.fail_create = False

    def create_run(self, **kwargs: Any) -> _Run:
        if self.fail_create:
            raise RuntimeError("run creation failed")
        run = _Run(
            id=self.next_id,
            task_id=kwargs["task_id"],
            input_text=kwargs.get("run_command").display_text,
            agent_id=kwargs.get("agent_id"),
            provider_id=kwargs.get("provider_id"),
            model_name=kwargs.get("model_name"),
            reasoning_effort=kwargs.get("reasoning_effort"),
        )
        self.next_id += 1
        self.runs[run.id] = run
        return run

    def get_run(self, run_id: int) -> _Run:
        return self.runs[run_id]

    def list_runs_for_task(self, task_id: int) -> list[_Run]:
        return [run for run in self.runs.values() if run.task_id == task_id]


class _RunState:
    def __init__(self, runs: _Runs) -> None:
        self.runs = runs

    def claim_pending_run(self, run_id: int) -> _Run | None:
        run = self.runs.runs[run_id]
        if run.status != ConversationRunStatus.PENDING.value:
            return None
        run.status = ConversationRunStatus.RUNNING.value
        return run

    def cancel_run_if_running(
        self, run_id: int, end_reason: str = "child_agent_closed"
    ) -> _Run | None:
        run = self.runs.runs[run_id]
        if run.status not in {
            ConversationRunStatus.PENDING.value,
            ConversationRunStatus.RUNNING.value,
        }:
            return None
        run.status = ConversationRunStatus.CANCELLED.value
        run.end_reason = end_reason
        return run

    cancel_run_for_startup_recovery = cancel_run_if_running


class _Executor:
    def __init__(self, runs: _Runs) -> None:
        self.runs = runs
        self.tasks: dict[int, asyncio.Task[Any]] = {}
        self.start_calls = 0

    def start_registered(self, run_id: int, runner: Any) -> asyncio.Task[Any]:
        self.start_calls += 1
        task = asyncio.create_task(runner(self.runs.get_run(run_id)))
        self.tasks[run_id] = task
        return task

    async def start(self, run_id: int, runner: Any) -> asyncio.Task[Any]:
        return self.start_registered(run_id, runner)


class _FailingFollowUpExecutor(_Executor):
    def __init__(self, runs: _Runs) -> None:
        super().__init__(runs)
        self.start_calls = 0

    def start_registered(self, run_id: int, runner: Any) -> asyncio.Task[Any]:
        self.start_calls += 1
        if self.start_calls == 2:
            raise RuntimeError("follow-up executor unavailable")
        task = asyncio.create_task(runner(self.runs.get_run(run_id)))
        self.tasks[run_id] = task
        return task

    async def start(self, run_id: int, runner: Any) -> asyncio.Task[Any]:
        return self.start_registered(run_id, runner)


class _ClaimBarrierState(_RunState):
    def __init__(self, runs: _Runs) -> None:
        super().__init__(runs)
        self.claim_entered = threading.Event()
        self.claim_release = threading.Event()

    def claim_pending_run(self, run_id: int) -> _Run | None:
        run = super().claim_pending_run(run_id)
        if run is not None:
            self.claim_entered.set()
            self.claim_release.wait()
        return run


class _BlockingClaimState(_RunState):
    def __init__(self, runs: _Runs) -> None:
        super().__init__(runs)
        self.claim_entered = threading.Event()
        self.claim_release = threading.Event()

    def claim_pending_run(self, run_id: int) -> _Run | None:
        self.claim_entered.set()
        self.claim_release.wait()
        return super().claim_pending_run(run_id)


async def _service() -> tuple[ChildAgentSessionService, _Tasks, _Runs, _Executor]:
    tasks = _Tasks()
    tasks.tasks[1] = _Task(id=1, workspace_id=3, title="parent", task_type="user")
    runs = _Runs()
    runs.runs[2] = _Run(id=2, task_id=1, status=ConversationRunStatus.RUNNING.value)
    tasks.run_records = runs.runs
    executor = _Executor(runs)
    service = ChildAgentSessionService(
        task_service=tasks,
        conversation_run_service=runs,
        conversation_run_state_service=_RunState(runs),
        conversation_run_executor=executor,
    )
    return service, tasks, runs, executor


@pytest.mark.asyncio
async def test_delegate_creates_refs_and_returns_before_child_completion() -> None:
    service, tasks, runs, executor = await _service()
    child_started = asyncio.Event()
    child_release = asyncio.Event()

    async def child_runner(run: _Run) -> None:
        child_started.set()
        await child_release.wait()

    result = await asyncio.to_thread(
        service.start_child,
        parent_task_id=1,
        parent_run_id=2,
        workspace_id=3,
        child_agent_id="child",
        title="review",
        prompt="inspect",
        tool_call_id="tool-1",
        run_callback=child_runner,
        runtime_event_loop=asyncio.get_running_loop(),
    )

    assert result.child_task_id in tasks.tasks
    assert result.child_run_id in runs.runs
    await asyncio.wait_for(child_started.wait(), timeout=1)
    assert runs.runs[result.child_run_id].status == ConversationRunStatus.RUNNING.value
    assert not child_release.is_set()
    child_release.set()
    await executor.tasks[result.child_run_id]


@pytest.mark.asyncio
async def test_close_is_owned_and_idempotent_and_send_does_not_mutate_context() -> None:
    service, tasks, runs, executor = await _service()

    async def child_runner(run: _Run) -> None:
        await asyncio.Event().wait()

    result = await asyncio.to_thread(
        service.start_child,
        parent_task_id=1,
        parent_run_id=2,
        workspace_id=3,
        child_agent_id="child",
        title="review",
        prompt="inspect",
        tool_call_id="tool-1",
        run_callback=child_runner,
        runtime_event_loop=asyncio.get_running_loop(),
    )
    await asyncio.sleep(0)

    sent = service.send(
        parent_task_id=1,
        parent_run_id=2,
        child_task_id=result.child_task_id,
        message="follow up",
        message_id="message-1",
    )
    assert sent.accepted_seq == 1

    closed = service.close(
        parent_task_id=1,
        parent_run_id=2,
        child_task_id=result.child_task_id,
    )
    assert closed.idempotent is False
    assert runs.runs[result.child_run_id].status == ConversationRunStatus.CANCELLED.value
    again = service.close(
        parent_task_id=1,
        parent_run_id=2,
        child_task_id=result.child_task_id,
    )
    assert again.idempotent is True
    with pytest.raises(ChildAgentSessionError, match="child_agent_session_closed"):
        service.send(
            parent_task_id=1,
            parent_run_id=2,
            child_task_id=result.child_task_id,
            message="late",
            message_id="message-2",
        )
    with pytest.raises(ChildAgentSessionError, match="child_agent_not_owned"):
        service.close(parent_task_id=9, parent_run_id=2, child_task_id=result.child_task_id)


@pytest.mark.asyncio
async def test_wait_uses_terminal_history_cursor_and_canonical_final_output() -> None:
    service, tasks, runs, _ = await _service()
    child = tasks.get_or_create_task(
        workspace_id=3, title="review", parent_task_id=1, parent_run_id=2, task_type="delegation"
    )
    first = runs.create_run(task_id=child.id, run_command=SimpleNamespace(display_text="inspect"))
    first.status = ConversationRunStatus.COMPLETED.value
    first.final_output = "first result"
    second = runs.create_run(task_id=child.id, run_command=SimpleNamespace(display_text="again"))
    second.status = ConversationRunStatus.FAILED.value
    second.end_reason = "runtime_failed"

    first_result = service.read_wait(
        parent_task_id=1,
        parent_run_id=2,
        targets=[{"child_task_id": child.id}],
        wait_mode="any",
    )
    assert first_result.value.messages[0].child_run_id == first.id
    assert first_result.value.messages[0].final_output == "first result"
    next_result = service.read_wait(
        parent_task_id=1,
        parent_run_id=2,
        targets=[{"child_task_id": child.id, "after_run_id": first.id}],
        wait_mode="any",
    )
    assert next_result.value.messages[0].child_run_id == second.id
    with pytest.raises(ChildAgentSessionError, match="child_agent_no_targets"):
        service.read_wait(
            parent_task_id=1,
            parent_run_id=2,
            targets=[],
            wait_mode="any",
        )
    with pytest.raises(ChildAgentSessionError, match="child_agent_duplicate_target"):
        service.read_wait(
            parent_task_id=1,
            parent_run_id=2,
            targets=[
                {"child_task_id": child.id},
                {"child_task_id": child.id},
            ],
            wait_mode="any",
        )
    with pytest.raises(ChildAgentSessionError, match="child_agent_invalid_cursor"):
        service.read_wait(
            parent_task_id=1,
            parent_run_id=2,
            targets=[{"child_task_id": child.id, "after_run_id": 9999}],
            wait_mode="any",
        )


def test_wait_all_requires_every_target_to_have_a_terminal_run() -> None:
    service, tasks, runs, _ = asyncio.run(_service())
    first_child = tasks.get_or_create_task(
        workspace_id=3, title="first", parent_task_id=1, parent_run_id=2, task_type="delegation"
    )
    second_child = tasks.get_or_create_task(
        workspace_id=3, title="second", parent_task_id=1, parent_run_id=2, task_type="delegation"
    )
    first_run = runs.create_run(
        task_id=first_child.id, run_command=SimpleNamespace(display_text="a")
    )
    first_run.status = ConversationRunStatus.COMPLETED.value
    second_run = runs.create_run(
        task_id=second_child.id, run_command=SimpleNamespace(display_text="b")
    )
    second_run.status = ConversationRunStatus.RUNNING.value

    pending = service.read_wait(
        parent_task_id=1,
        parent_run_id=2,
        targets=[
            {"child_task_id": first_child.id},
            {"child_task_id": second_child.id},
        ],
        wait_mode="all",
    )
    assert pending.ready is False
    assert [item.child_task_id for item in pending.value.pending] == [second_child.id]

    second_run.status = ConversationRunStatus.FAILED.value
    completed = service.read_wait(
        parent_task_id=1,
        parent_run_id=2,
        targets=[
            {"child_task_id": first_child.id},
            {"child_task_id": second_child.id},
        ],
        wait_mode="all",
    )
    assert completed.ready is True
    assert {item.child_task_id for item in completed.value.messages} == {
        first_child.id,
        second_child.id,
    }


@pytest.mark.asyncio
async def test_child_finalization_notifies_waiter_and_parent_close_converges_run() -> None:
    service, tasks, runs, executor = await _service()
    release = asyncio.Event()

    async def child_runner(run: _Run) -> None:
        await release.wait()

    started = await asyncio.to_thread(
        service.start_child,
        parent_task_id=1,
        parent_run_id=2,
        workspace_id=3,
        child_agent_id="child",
        title="review",
        prompt="inspect",
        tool_call_id="tool-1",
        run_callback=child_runner,
        runtime_event_loop=asyncio.get_running_loop(),
    )
    child_task = tasks.get_task(started.child_task_id)

    waiter = asyncio.create_task(
        service.wait_coordinator.wait_async(
            2,
            lambda: service.read_wait(
                parent_task_id=1,
                parent_run_id=2,
                targets=[{"child_task_id": child_task.id}],
                wait_mode="any",
            ),
            timeout_seconds=1,
        )
    )
    await asyncio.sleep(0)
    run = runs.runs[started.child_run_id]
    run.status = ConversationRunStatus.COMPLETED.value
    run.final_output = "canonical output"
    service.notify_waiters(2)
    outcome = await waiter
    assert outcome.value.messages[0].final_output == "canonical output"

    service.close_children(2)
    assert runs.runs[started.child_run_id].status == ConversationRunStatus.COMPLETED.value
    release.set()
    await executor.tasks[started.child_run_id]


@pytest.mark.asyncio
async def test_parent_finalization_observer_closes_child_and_recovery_is_idempotent() -> None:
    service, tasks, runs, executor = await _service()

    async def child_runner(run: _Run) -> None:
        await asyncio.Event().wait()

    started = await asyncio.to_thread(
        service.start_child,
        parent_task_id=1,
        parent_run_id=2,
        workspace_id=3,
        child_agent_id="child",
        title="review",
        prompt="inspect",
        tool_call_id="tool-observer",
        run_callback=child_runner,
        runtime_event_loop=asyncio.get_running_loop(),
    )
    ChildAgentRunFinalizationObserver(service).on_run_finalized(
        SimpleNamespace(id=2, task_id=1)
    )
    await asyncio.sleep(0.05)
    assert runs.runs[started.child_run_id].status == ConversationRunStatus.CANCELLED.value
    await asyncio.gather(executor.tasks[started.child_run_id], return_exceptions=True)

    recovered = ChildAgentRecoveryHook(
        task_service=tasks,
        run_state_service=_RunState(runs),
    ).recover([SimpleNamespace(id=2, task_id=1)])
    assert recovered == 0

    orphan = tasks.get_or_create_task(
        workspace_id=3,
        title="orphan",
        parent_task_id=1,
        parent_run_id=2,
        task_type="delegation",
    )
    orphan_run = runs.create_run(
        task_id=orphan.id,
        run_command=SimpleNamespace(display_text="orphan"),
    )
    orphan_run.status = ConversationRunStatus.RUNNING.value
    recovered = ChildAgentRecoveryHook(
        task_service=tasks,
        run_state_service=_RunState(runs),
    ).recover([SimpleNamespace(id=2, task_id=1)])
    assert recovered == 1
    assert orphan_run.status == ConversationRunStatus.CANCELLED.value
    assert (
        ChildAgentRecoveryHook(
            task_service=tasks,
            run_state_service=_RunState(runs),
        ).recover([SimpleNamespace(id=2, task_id=1)])
        == 0
    )


def test_checkpoint_contains_only_child_locator_fields() -> None:
    state = ReactGraphState(
        step_count=0,
        tool_error_count=0,
        requested_tool=False,
        final_response=False,
        terminal=False,
        max_steps=5,
        final_text="",
        last_tool_results={},
        child_agents={
            "tool-1": {
                "child_task_id": 101,
                "child_run_id": 202,
                "agent_id": "reviewer",
                "title": "Review",
                "status": "running",
            }
        },
    )
    payload = state.model_dump(mode="json")
    assert payload["child_agents"]["tool-1"] == {
        "child_task_id": 101,
        "child_run_id": 202,
        "agent_id": "reviewer",
        "title": "Review",
        "status": "running",
    }
    assert "mailbox" not in payload["child_agents"]["tool-1"]


@pytest.mark.asyncio
async def test_close_fence_wins_after_claim_before_executor_start() -> None:
    service, tasks, runs, executor = await _service()
    state = _ClaimBarrierState(runs)
    service._run_state = state

    async def child_runner(run: _Run) -> None:
        await asyncio.Event().wait()

    start_task = asyncio.create_task(
        asyncio.to_thread(
            service.start_child,
            parent_task_id=1,
            parent_run_id=2,
            workspace_id=3,
            child_agent_id="child",
            title="review",
            prompt="inspect",
            tool_call_id="close-race",
            run_callback=child_runner,
            runtime_event_loop=asyncio.get_running_loop(),
        )
    )
    await asyncio.to_thread(state.claim_entered.wait)
    child = next(task for task in tasks.tasks.values() if task.id != 1)
    try:
        closed = await asyncio.wait_for(
            asyncio.to_thread(
                service.close,
                parent_task_id=1,
                parent_run_id=2,
                child_task_id=child.id,
            ),
            timeout=1,
        )
    finally:
        state.claim_release.set()

    with pytest.raises(ChildAgentSessionError, match="child_agent_start_failed"):
        await start_task
    assert closed.status == ConversationRunStatus.CANCELLED.value
    assert executor.start_calls == 0
    assert executor.tasks == {}


@pytest.mark.asyncio
async def test_run_creation_failure_reclaims_new_child_task() -> None:
    service, tasks, runs, _ = await _service()
    runs.fail_create = True

    async def child_runner(run: _Run) -> None:
        return None

    with pytest.raises(ChildAgentSessionError, match="child_agent_start_failed"):
        await asyncio.to_thread(
            service.start_child,
            parent_task_id=1,
            parent_run_id=2,
            workspace_id=3,
            child_agent_id="child",
            title="reclaim me",
            prompt="inspect",
            tool_call_id="run-create-failure",
            run_callback=child_runner,
            runtime_event_loop=asyncio.get_running_loop(),
        )

    assert set(tasks.tasks) == {1}
    assert set(runs.runs) == {2}


@pytest.mark.asyncio
async def test_finalization_observer_only_schedules_blocking_canonical_work() -> None:
    service, tasks, _, _ = await _service()
    tasks.block_get = True
    observer = ChildAgentRunFinalizationObserver(service)
    returned = threading.Event()

    def invoke() -> None:
        observer.on_run_finalized(SimpleNamespace(id=2, task_id=1))
        returned.set()

    invocation = asyncio.create_task(asyncio.to_thread(invoke))
    try:
        await asyncio.wait_for(asyncio.to_thread(returned.wait), timeout=1)
    finally:
        tasks.get_release.set()
        await invocation


@pytest.mark.asyncio
async def test_close_reclaims_session_indexes() -> None:
    service, tasks, _, _ = await _service()

    async def child_runner(run: _Run) -> None:
        await asyncio.Event().wait()

    started = await asyncio.to_thread(
        service.start_child,
        parent_task_id=1,
        parent_run_id=2,
        workspace_id=3,
        child_agent_id="child",
        title="close me",
        prompt="inspect",
        tool_call_id="close-indexes",
        run_callback=child_runner,
        runtime_event_loop=asyncio.get_running_loop(),
    )
    result = await asyncio.to_thread(
        service.close,
        parent_task_id=1,
        parent_run_id=2,
        child_task_id=started.child_task_id,
    )

    assert result.status == ConversationRunStatus.CANCELLED.value
    assert started.child_task_id not in service._by_child_task
    assert (2, "close-indexes") not in service._sessions
    execution = service._executor.tasks[started.child_run_id]
    await asyncio.gather(execution, return_exceptions=True)


@pytest.mark.asyncio
async def test_follow_up_failure_keeps_mailbox_and_converges_created_run() -> None:
    service, tasks, runs, _ = await _service()
    failing_executor = _FailingFollowUpExecutor(runs)
    service._executor = failing_executor
    release = asyncio.Event()

    async def child_runner(run: _Run) -> None:
        await release.wait()

    started = await asyncio.to_thread(
        service.start_child,
        parent_task_id=1,
        parent_run_id=2,
        workspace_id=3,
        child_agent_id="child",
        title="review",
        prompt="inspect",
        tool_call_id="follow-up-failure",
        run_callback=child_runner,
        runtime_event_loop=asyncio.get_running_loop(),
    )
    runs.runs[started.child_run_id].status = ConversationRunStatus.COMPLETED.value
    service.send(
        parent_task_id=1,
        parent_run_id=2,
        child_task_id=started.child_task_id,
        message="follow up",
        message_id="follow-up-message",
    )
    await asyncio.sleep(0.05)

    session = service._by_child_task[started.child_task_id]
    follow_up_runs = [
        run
        for run in runs.list_runs_for_task(started.child_task_id)
        if run.id != started.child_run_id
    ]
    assert len(follow_up_runs) == 1
    assert follow_up_runs[0].status == ConversationRunStatus.CANCELLED.value
    assert [item.message_id for item in session.mailbox] == ["follow-up-message"]
    assert session.follow_up_pending is True
    release.set()
    await failing_executor.tasks[started.child_run_id]


@pytest.mark.asyncio
async def test_child_finalization_and_send_schedule_one_follow_up() -> None:
    service, tasks, runs, executor = await _service()
    release = asyncio.Event()

    async def child_runner(run: _Run) -> None:
        await release.wait()

    started = await asyncio.to_thread(
        service.start_child,
        parent_task_id=1,
        parent_run_id=2,
        workspace_id=3,
        child_agent_id="child",
        title="review",
        prompt="inspect",
        tool_call_id="finalization-race",
        run_callback=child_runner,
        runtime_event_loop=asyncio.get_running_loop(),
    )
    child = tasks.get_task(started.child_task_id)
    runs.runs[started.child_run_id].status = ConversationRunStatus.COMPLETED.value
    service.send(
        parent_task_id=1,
        parent_run_id=2,
        child_task_id=child.id,
        message="follow up",
        message_id="finalization-message",
    )
    service.notify_child_finalized(runs.runs[started.child_run_id])
    await asyncio.sleep(0.05)

    child_runs = runs.list_runs_for_task(child.id)
    assert len(child_runs) == 2
    assert executor.tasks.get(child_runs[-1].id) is not None
    release.set()
    await asyncio.gather(*executor.tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_send_rechecks_terminal_child_after_finalization_race() -> None:
    service, tasks, runs, executor = await _service()
    release = asyncio.Event()
    read_entered = threading.Event()
    read_release = threading.Event()
    original_list = runs.list_runs_for_task
    first_read = True

    async def child_runner(run: _Run) -> None:
        await release.wait()

    started = await asyncio.to_thread(
        service.start_child,
        parent_task_id=1,
        parent_run_id=2,
        workspace_id=3,
        child_agent_id="child",
        title="review",
        prompt="inspect",
        tool_call_id="send-finalization-race",
        run_callback=child_runner,
        runtime_event_loop=asyncio.get_running_loop(),
    )
    child = tasks.get_task(started.child_task_id)

    def list_runs_with_race(child_task_id: int) -> list[_Run]:
        nonlocal first_read
        listed = original_list(child_task_id)
        if child_task_id == child.id and first_read:
            first_read = False
            snapshot = [SimpleNamespace(**vars(run)) for run in listed]
            read_entered.set()
            if not read_release.wait(timeout=2):
                raise RuntimeError("list barrier timed out")
            return snapshot
        return listed

    runs.list_runs_for_task = list_runs_with_race  # type: ignore[method-assign]
    send_task = asyncio.create_task(
        asyncio.to_thread(
            service.send,
            parent_task_id=1,
            parent_run_id=2,
            child_task_id=child.id,
            message="follow up",
            message_id="race-message",
        )
    )
    await asyncio.to_thread(read_entered.wait)
    runs.runs[started.child_run_id].status = ConversationRunStatus.COMPLETED.value
    finalization_task = asyncio.create_task(
        asyncio.to_thread(service.notify_child_finalized, runs.runs[started.child_run_id])
    )
    read_release.set()
    await send_task
    await finalization_task
    await asyncio.sleep(0.05)

    assert len(runs.list_runs_for_task(child.id)) == 2
    release.set()
    await asyncio.gather(*executor.tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_launch_claim_does_not_block_event_loop() -> None:
    service, _, runs, _ = await _service()
    state = _BlockingClaimState(runs)
    service._run_state = state

    async def child_runner(run: _Run) -> None:
        return None

    start_task = asyncio.create_task(
        asyncio.to_thread(
            service.start_child,
            parent_task_id=1,
            parent_run_id=2,
            workspace_id=3,
            child_agent_id="child",
            title="review",
            prompt="inspect",
            tool_call_id="nonblocking-claim",
            run_callback=child_runner,
            runtime_event_loop=asyncio.get_running_loop(),
        )
    )
    await asyncio.to_thread(state.claim_entered.wait)
    threading.Timer(0.2, state.claim_release.set).start()
    heartbeat = asyncio.Event()
    heartbeat_task = asyncio.create_task(_set_event(heartbeat))
    started_at = time.monotonic()
    await asyncio.wait_for(heartbeat.wait(), timeout=0.5)
    assert time.monotonic() - started_at < 0.1
    await heartbeat_task
    await start_task


async def _set_event(event: asyncio.Event) -> None:
    await asyncio.sleep(0)
    event.set()


@pytest.mark.asyncio
async def test_start_failure_removes_session_and_cancellation_ghosts() -> None:
    service, tasks, runs, _ = await _service()

    class _FailingExecutor(_Executor):
        def start_registered(self, run_id: int, runner: Any) -> asyncio.Task[Any]:
            raise RuntimeError("executor start failed")

    service._executor = _FailingExecutor(runs)
    cancellation_registry.clear(201)

    async def child_runner(run: _Run) -> None:
        return None

    with pytest.raises(ChildAgentSessionError, match="child_agent_start_failed"):
        await asyncio.to_thread(
            service.start_child,
            parent_task_id=1,
            parent_run_id=2,
            workspace_id=3,
            child_agent_id="child",
            title="review",
            prompt="inspect",
            tool_call_id="start-failure-cleanup",
            run_callback=child_runner,
            runtime_event_loop=asyncio.get_running_loop(),
        )
    child = next(task for task in tasks.tasks.values() if task.id != 1)
    child_run = runs.list_runs_for_task(child.id)[0]
    assert child_run.status == ConversationRunStatus.CANCELLED.value
    assert child.id not in service._by_child_task
    assert not cancellation_registry.is_cancelled(child_run.id)


def test_non_delegation_child_is_not_owned() -> None:
    service, tasks, runs, _ = asyncio.run(_service())
    child = tasks.get_or_create_task(
        workspace_id=3,
        title="not a child session",
        parent_task_id=1,
        parent_run_id=2,
        task_type="user",
    )
    run = runs.create_run(task_id=child.id, run_command=SimpleNamespace(display_text="x"))
    run.status = ConversationRunStatus.COMPLETED.value

    with pytest.raises(ChildAgentSessionError, match="child_agent_not_owned"):
        service.status(parent_task_id=1, parent_run_id=2, child_task_id=child.id)


def test_wait_any_orders_by_created_at_then_run_id_then_child_task_id() -> None:
    service, tasks, runs, _ = asyncio.run(_service())
    first_child = tasks.get_or_create_task(
        workspace_id=3, title="first", parent_task_id=1, parent_run_id=2, task_type="delegation"
    )
    second_child = tasks.get_or_create_task(
        workspace_id=3, title="second", parent_task_id=1, parent_run_id=2, task_type="delegation"
    )
    first_run = runs.create_run(
        task_id=first_child.id, run_command=SimpleNamespace(display_text="a")
    )
    first_run.status = ConversationRunStatus.COMPLETED.value
    first_run.created_at = 20
    second_run = runs.create_run(
        task_id=second_child.id, run_command=SimpleNamespace(display_text="b")
    )
    second_run.status = ConversationRunStatus.COMPLETED.value
    second_run.created_at = 10

    result = service.read_wait(
        parent_task_id=1,
        parent_run_id=2,
        targets=[{"child_task_id": first_child.id}, {"child_task_id": second_child.id}],
        wait_mode="any",
    )
    assert result.value.messages[0].child_task_id == second_child.id


@pytest.mark.asyncio
async def test_executor_close_does_not_suppress_missing_child_session_contract() -> None:
    executor = object.__new__(ConversationRunExecutor)
    executor._child_sessions = SimpleNamespace(
        shutdown=lambda: (_ for _ in ()).throw(AttributeError("missing shutdown"))
    )
    executor._executions = {}

    with pytest.raises(AttributeError, match="missing shutdown"):
        await executor.close()


def test_reset_service_dependencies_clears_child_agent_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.service import depends

    cleared: list[str] = []
    monkeypatch.setattr(
        depends.get_async_child_agent_wait_coordinator,
        "cache_clear",
        lambda: cleared.append("wait_coordinator"),
    )
    monkeypatch.setattr(
        depends.get_child_agent_session_service,
        "cache_clear",
        lambda: cleared.append("session_service"),
    )
    depends.reset_service_dependencies()

    assert cleared == ["wait_coordinator", "session_service"]
