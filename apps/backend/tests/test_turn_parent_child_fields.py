"""Child turn linkage tests."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.service import depends as service_depends
from app.service.task.turn_service import TurnService
from app.storage.model.task_model import TaskModel
from app.storage.model.workspace_model import WorkspaceModel
from app.storage.store_engines import close_storage, init_storage, main_session_factory
from app.utils.datetime_utils import to_text, utc_now


@pytest.fixture
def isolated_storage(tmp_path: Path) -> Iterator[None]:
    """为单个测试初始化并清理隔离 SQLite 存储生命周期。

    参数:
        tmp_path: pytest 提供的测试临时目录。

    返回:
        已初始化存储的测试上下文。

    异常:
        OSError: 临时 SQLite 文件无法初始化时抛出。

    副作用:
        临时覆盖进程存储配置，并在测试结束后关闭存储和恢复原配置。
    """

    original_database_file = Settings.DATABASE_FILE
    original_log_database_file = Settings.LOG_DATABASE_FILE
    original_checkpoint_file = Settings.CHECKPOINT_FILE
    service_depends.reset_service_dependencies()
    close_storage()
    Settings.override(
        DATABASE_FILE=tmp_path / "app.sqlite3",
        LOG_DATABASE_FILE=tmp_path / "logs.sqlite3",
        CHECKPOINT_FILE=tmp_path / "checkpoints.sqlite3",
    )
    init_storage()
    try:
        yield
    finally:
        service_depends.reset_service_dependencies()
        close_storage()
        Settings.override(
            DATABASE_FILE=original_database_file,
            LOG_DATABASE_FILE=original_log_database_file,
            CHECKPOINT_FILE=original_checkpoint_file,
        )


def test_create_child_turn_records_parent_and_delegation(isolated_storage):
    """验证子轮次持久化父子关联且不更新任务最新轮次。

    参数:
        isolated_storage: 已初始化的隔离 SQLite 存储 fixture。

    返回:
        无。

    异常:
        无；断言失败时由 pytest 报告。

    副作用:
        向隔离数据库写入工作区、任务和一个委派子轮次。
    """

    now = to_text(utc_now())
    with main_session_factory().begin() as session:
        session.add(
            WorkspaceModel(
                workspace_id="workspace_1",
                root_path="H:/workspace",
                name="workspace",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            TaskModel(
                task_id="task_1",
                workspace_id="workspace_1",
                agent_id="developer",
                title="parent task",
                status="open",
                created_at=now,
                updated_at=now,
            )
        )
    service = TurnService()
    child = service.create_child_turn(
        task_id="task_1",
        input_text="review this",
        agent_id="delegate_reviewer",
        parent_turn_id="turn_parent",
        delegation_id="del_1",
    )

    assert child.parent_turn_id == "turn_parent"
    assert child.delegation_id == "del_1"
    assert child.agent_id == "delegate_reviewer"
