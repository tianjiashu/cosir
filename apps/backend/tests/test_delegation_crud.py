"""delegation storage tests."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.models.delegation_record import DelegationRecord
from app.service.depends import reset_service_dependencies
from app.storage.crud.delegation_crud import DelegationCrud
from app.storage.store_engines import close_storage, init_storage
from app.utils.datetime_utils import utc_now


@pytest.fixture
def isolated_storage(tmp_path: Path) -> Iterator[None]:
    """Initialize an isolated SQLite storage lifecycle for one test.

    参数:
        tmp_path: pytest 提供的当前测试临时目录。

    返回:
        已初始化的隔离存储上下文。

    异常:
        OSError: 如果临时 SQLite 存储无法初始化。

    副作用:
        临时覆盖进程级存储路径，并在测试结束时关闭存储和恢复原配置。
    """

    original_database_file = Settings.DATABASE_FILE
    original_log_database_file = Settings.LOG_DATABASE_FILE
    original_checkpoint_file = Settings.CHECKPOINT_FILE
    reset_service_dependencies()
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
        reset_service_dependencies()
        close_storage()
        Settings.override(
            DATABASE_FILE=original_database_file,
            LOG_DATABASE_FILE=original_log_database_file,
            CHECKPOINT_FILE=original_checkpoint_file,
        )


def test_delegation_crud_create_update_and_list(isolated_storage):
    now = utc_now()
    crud = DelegationCrud()
    record = DelegationRecord(
        delegation_id="del_1",
        task_id="task_1",
        parent_turn_id="turn_parent",
        child_turn_id="",
        parent_agent_id="developer",
        child_agent_id="delegate_reviewer",
        delegation_type="review",
        status="pending",
        prompt="review this",
        summary="",
        error="",
        requested_tools=("read_file", "search_files"),
        effective_tools=("read_file",),
        created_at=now,
        updated_at=now,
    )

    crud.create(record)
    crud.update_status("del_1", status="running", child_turn_id="turn_child")

    loaded = crud.get("del_1")
    assert loaded.status == "running"
    assert loaded.child_turn_id == "turn_child"
    assert crud.list_by_parent_turn("turn_parent")[0].delegation_id == "del_1"
    assert crud.list_pending_or_running()[0].delegation_id == "del_1"
