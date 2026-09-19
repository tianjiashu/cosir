from __future__ import annotations

import threading
from pathlib import Path

from app.core.tools.guard.file_resource_paths import FileResourcePaths
from app.core.tools.guard.file_state.file_path_lock_registry import FilePathLockRegistry
from app.core.tools.guard.file_tool_state_coordinator import (
    FileToolExecutionPlan,
    FileToolStateCoordinator,
)
from app.core.tools.schemas import ToolExecutionContext


def test_active_path_lock_set_can_exceed_idle_cache_limit_without_losing_coordination(
    tmp_path: Path,
) -> None:
    registry = FilePathLockRegistry(max_paths_per_task=2)
    paths = [tmp_path / f"path-{index}" for index in range(5)]
    holder_ready = threading.Event()
    release_holder = threading.Event()
    contender_acquired = threading.Event()

    def hold_all_paths() -> None:
        with registry.acquire("task", paths):
            holder_ready.set()
            assert release_holder.wait(timeout=5)

    def acquire_one_held_path() -> None:
        with registry.acquire("task", [paths[-1]]):
            contender_acquired.set()

    holder = threading.Thread(target=hold_all_paths)
    contender = threading.Thread(target=acquire_one_held_path)
    holder.start()
    try:
        assert holder_ready.wait(timeout=2)
        contender.start()
        assert not contender_acquired.wait(timeout=0.05)
    finally:
        release_holder.set()
    holder.join(timeout=2)
    contender.join(timeout=2)

    assert not holder.is_alive()
    assert not contender.is_alive()
    assert contender_acquired.is_set()
    assert len(registry._tasks["task"].paths) <= 2


def test_shared_workspace_path_lock_serializes_different_tasks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "shared.txt"
    plan = FileToolExecutionPlan(
        resources=FileResourcePaths(write_paths=(target,), lock_paths=(workspace, target)),
        observed_paths=(),
    )
    first_context = ToolExecutionContext(
        task_id=101,
        workspace_id=1,
        workspace_root=workspace,
    )
    second_context = ToolExecutionContext(
        task_id=202,
        workspace_id=1,
        workspace_root=workspace,
    )
    first_coordinator = FileToolStateCoordinator()
    second_coordinator = FileToolStateCoordinator()
    first_entered = threading.Event()
    release_first = threading.Event()
    second_attempting = threading.Event()
    second_entered = threading.Event()

    def hold_first() -> None:
        with first_coordinator.lock(plan, first_context):
            first_entered.set()
            assert release_first.wait(timeout=5)

    def acquire_from_second_task() -> None:
        second_attempting.set()
        with second_coordinator.lock(plan, second_context):
            second_entered.set()

    first = threading.Thread(target=hold_first)
    second = threading.Thread(target=acquire_from_second_task)
    first.start()
    assert first_entered.wait(timeout=3)
    second.start()
    assert second_attempting.wait(timeout=3)
    try:
        assert not second_entered.wait(timeout=0.05)
    finally:
        release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert second_entered.is_set()
