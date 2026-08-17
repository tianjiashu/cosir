"""task-level context usage persistence and return tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.api.tasks_api import get_task
from app.config.configuration import build_agent_registry, get_agent_registry, set_agent_registry
from app.config.settings import Settings
from app.core.llm.context_window_resolver import resolve_context_window
from app.core.workflows.nodes import model_node
from app.models import TaskRecord
from app.models.context_usage import ContextUsage
from app.service.depends import (
    close_service_dependencies,
    initialize_service_dependencies,
    reset_service_dependencies,
)
from app.service.task.task_service import TaskService
from app.service.task.workspace_service import WorkspaceService
from app.storage.store_engines import init_storage


@pytest.fixture
def storage_stack(tmp_path: Path):
    db_file = tmp_path / "app.sqlite3"
    log_db_file = tmp_path / "logs.sqlite3"
    checkpoint_file = tmp_path / "langgraph_checkpoints.sqlite"
    log_dir = tmp_path / "logs"
    Settings.override(
        DATABASE_FILE=db_file,
        LOG_DATABASE_FILE=log_db_file,
        CHECKPOINT_FILE=checkpoint_file,
        LOG_DIR=log_dir,
    )
    init_storage()
    initialize_service_dependencies()
    set_agent_registry(build_agent_registry())
    yield
    close_service_dependencies()
    reset_service_dependencies()
    Settings.load()


def _create_workspace() -> str:
    workspace = WorkspaceService().create_workspace(
        name="ws_ctx_usage", root_path="H:/ws_ctx_usage",
    )
    return workspace.workspace_id


def _create_task() -> TaskRecord:
    task_service = TaskService()
    task, _ = task_service.create_task_with_initial_turn(
        workspace_id=_create_workspace(), agent_id="developer",
        input_text="ctx usage task",
    )
    return task


def test_update_context_usage_persists_used_and_bumps_updated_at(storage_stack: Path) -> None:
    task = _create_task()
    task_id = task.task_id
    before_updated_at = task.updated_at
    updated = TaskService().update_context_usage(task_id, 1234)
    assert isinstance(updated, TaskRecord)
    assert updated.task_id == task_id
    assert updated.context_usage_used == 1234
    assert updated.updated_at >= before_updated_at


def test_update_context_usage_roundtrip_via_get(storage_stack: Path) -> None:
    task = _create_task()
    TaskService().update_context_usage(task.task_id, 42)
    reread = TaskService().get_task(task.task_id)
    assert reread.context_usage_used == 42


def test_update_context_usage_unknown_task_raises(storage_stack: Path) -> None:
    with pytest.raises(KeyError):
        TaskService().update_context_usage("task_does_not_exist", 1)


def test_emit_context_usage_writes_back_task(storage_stack: Path) -> None:
    task = _create_task()
    usage = ContextUsage(used_tokens=777, total_tokens=8000)
    meter = MagicMock()
    meter.read.return_value = usage
    runtime_context = MagicMock()
    runtime_context.usage_meter = meter
    with patch("app.core.workflows.nodes.model_node.get_task_service") as mock_get, patch(
        "app.core.workflows.nodes.model_node._runtime_context", return_value=runtime_context
    ), patch("app.core.workflows.nodes.model_node.write_event") as mock_write:
        mock_service = MagicMock()
        mock_get.return_value = mock_service
        model_node._emit_context_usage(step_id="step_1", task_id=task.task_id)
        mock_write.assert_called_once()
        mock_service.update_context_usage.assert_called_once_with(task.task_id, 777)


def test_emit_context_usage_service_failure_only_logs(storage_stack: Path) -> None:
    task = _create_task()
    usage = ContextUsage(used_tokens=1, total_tokens=1)
    meter = MagicMock()
    meter.read.return_value = usage
    runtime_context = MagicMock()
    runtime_context.usage_meter = meter
    with patch("app.core.workflows.nodes.model_node.get_task_service") as mock_get, patch(
        "app.core.workflows.nodes.model_node._runtime_context", return_value=runtime_context
    ), patch("app.core.workflows.nodes.model_node.write_event"):
        mock_service = MagicMock()
        mock_service.update_context_usage.side_effect = RuntimeError("boom")
        mock_get.return_value = mock_service
        model_node._emit_context_usage(step_id="step_x", task_id=task.task_id)
        mock_service.update_context_usage.assert_called_once()


def test_get_task_returns_context_window_total(storage_stack: Path) -> None:
    task = _create_task()
    model_name = get_agent_registry().resolve(task.agent_id).model_name
    expected_total = resolve_context_window(model_name)
    resp = asyncio.run(get_task(task.task_id, TaskService()))
    assert resp.context_usage_used is None
    assert resp.context_window_total == expected_total


def test_get_task_unknown_agent_degrades_to_none_total(storage_stack: Path) -> None:
    task = _create_task()
    bad_record = TaskRecord(
        task_id=task.task_id, workspace_id=task.workspace_id,
        agent_id="nonexistent_agent", title=task.title,
        status=task.status, execution_status=task.execution_status,
        task_type=task.task_type, parent_task_id=task.parent_task_id,
        parent_turn_id=task.parent_turn_id, delegation_id=task.delegation_id,
        context_usage_used=task.context_usage_used,
        created_at=task.created_at, updated_at=task.updated_at,
    )
    with patch(
        "app.service.task.task_service.TaskService.get_task", return_value=bad_record
    ), patch("app.config.configuration.get_agent_registry") as mock_registry:
        mock_registry.return_value.resolve.return_value = None
        resp = asyncio.run(get_task(task.task_id, TaskService()))
        assert resp.context_window_total is None
