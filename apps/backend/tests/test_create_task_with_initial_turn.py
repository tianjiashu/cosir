"""create_task_with_initial_turn 业务编排单元测试。

验证 `TaskService.create_task_with_initial_turn` 在单一调用内完成：
  1. 创建顶层任务（标题由 input_text 派生，任务层不持有用户输入文本）。
  2. 创建首个 pending 轮次（input_text 归属轮次维度）。
  3. 返回 `(task, turn)` 二元组且两者关联一致。

装配不变量：
  - Settings.override 将全部 SQLite 库指向 tmp_path，不污染开发库。
  - init_storage() 建表 → initialize_service_dependencies() 装配 service 单例。
  - 先建 workspace（create_task 要求 workspace 存在），再触发组合方法。
  - teardown 关闭存储并复位全局单例与 Settings。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from app.config.configuration import build_agent_registry, set_agent_registry
from app.config.settings import Settings
from app.models import TaskRecord, TurnRecord
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
    """装配指向 tmp_path 的真实存储与服务单例，测试结束复位全局状态。"""
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
    """创建一个测试工作区并返回其标识。"""
    workspace = WorkspaceService().create_workspace(
        name="ws_create_initial_turn",
        root_path="H:/ws_create_initial_turn",
    )
    return workspace.workspace_id


def test_creates_task_and_initial_pending_turn(storage_stack: None) -> None:
    """组合方法应创建任务与首个 pending 轮次，且两者关联一致。"""
    workspace_id = _create_workspace()
    task_service = TaskService()

    task, turn = task_service.create_task_with_initial_turn(
        workspace_id=workspace_id,
        agent_id="developer",
        input_text="实现登录接口",
    )

    assert isinstance(task, TaskRecord)
    assert isinstance(turn, TurnRecord)
    assert turn.task_id == task.task_id
    assert turn.status == "pending"
    assert turn.input_text == "实现登录接口"
    # 任务标题应由 input_text 派生，任务层不持有用户输入文本
    assert task.title == "实现登录接口"


def test_requires_non_empty_input_text(storage_stack: None) -> None:
    """空白 input_text 应抛出 ValueError，不创建任何记录。"""
    workspace_id = _create_workspace()
    task_service = TaskService()

    with pytest.raises(ValueError):
        task_service.create_task_with_initial_turn(
            workspace_id=workspace_id,
            agent_id="developer",
            input_text="   ",
        )


def test_rejects_unknown_workspace(storage_stack: None) -> None:
    """不存在的工作区应触发外键完整性错误，不创建任何记录。"""
    task_service = TaskService()

    with pytest.raises(IntegrityError):  # 底层 create_task 经外键约束抛出
        task_service.create_task_with_initial_turn(
            workspace_id="nonexistent-workspace-id",
            agent_id="developer",
            input_text="任意输入",
        )


def test_rejects_unregistered_agent(storage_stack: None) -> None:
    """未注册的 agent_id 应抛出 ValueError，不创建任何记录。"""
    workspace_id = _create_workspace()
    task_service = TaskService()

    with pytest.raises(ValueError):
        task_service.create_task_with_initial_turn(
            workspace_id=workspace_id,
            agent_id="no-such-agent",
            input_text="任意输入",
        )


def test_compensates_orphan_task_on_turn_failure(storage_stack: None) -> None:
    """首轮次写入失败时，已创建的 task 应被补偿删除，不留孤儿任务。"""
    from app.service.task.task_service import TaskService as _TS

    workspace_id = _create_workspace()
    task_service = _TS()
    original_create = task_service._turn.create

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated turn persist failure")

    task_service._turn.create = _boom  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            task_service.create_task_with_initial_turn(
                workspace_id=workspace_id,
                agent_id="developer",
                input_text="任意输入",
            )
    finally:
        task_service._turn.create = original_create  # type: ignore[assignment]

    # 补偿删除后，任务表应无残留
    from app.service.task.task_service import TaskService as _TS2

    remaining = _TS2().list_tasks_for_workspace(workspace_id)
    assert remaining == []
