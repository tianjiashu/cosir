"""验证 provisional Task 的创建标记、受控清理与附件保留边界。"""

from pathlib import Path

import pytest

from app.models.errors.deletion_errors import ProvisionalTaskCleanupConflictError
from app.service.depends import (
    close_service_dependencies,
    get_conversation_run_crud,
    get_task_crud,
    get_task_service,
    get_workspace_service,
)
from app.storage.store_engines import init_storage
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.utils import paths


@pytest.fixture
def storage(tmp_path: Path):
    """为 provisional Task 测试提供隔离数据库和附件目录。"""

    close_service_dependencies()
    db_dir = tmp_path / "storage"
    db_dir.mkdir()
    paths.override(
        DATABASE_FILE=db_dir / "app.sqlite3",
        CHECKPOINT_FILE=db_dir / "checkpoints.sqlite3",
        LOG_DIR=db_dir / "logs",
    )
    init_storage()
    task_runtime_spaces.close()
    yield tmp_path
    task_runtime_spaces.close()
    close_service_dependencies()
    paths.reset()


def _workspace(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    return get_workspace_service().create_workspace("workspace", str(root))


def test_create_task_preserves_creation_command_id(storage: Path) -> None:
    workspace = _workspace(storage)

    task = get_workspace_service().create_task(
        workspace.id,
        "new task",
        creation_command_id="creation-1",
    )

    assert get_task_crud().get(task.id).creation_command_id == "creation-1"


def test_cleanup_provisional_task_keeps_uploaded_attachment_and_skips_gc(
    storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(storage)
    task = get_workspace_service().create_task(
        workspace.id,
        "new task",
        creation_command_id="creation-1",
    )
    attachment = Path(workspace.root_path) / ".cosir" / "Attachment" / "image.png"
    attachment.write_bytes(b"image")
    service = get_task_service()

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Task deletion must not run attachment orphan GC")

    monkeypatch.setattr(service, "collect_workspace_attachment_orphans", fail_if_called)
    service.cleanup_provisional_task(task.id, "creation-1")

    with pytest.raises(KeyError):
        get_task_crud().get(task.id)
    assert attachment.exists()


def test_cleanup_provisional_task_rejects_mismatched_marker(storage: Path) -> None:
    workspace = _workspace(storage)
    task = get_workspace_service().create_task(
        workspace.id,
        "new task",
        creation_command_id="creation-1",
    )

    with pytest.raises(ProvisionalTaskCleanupConflictError):
        get_task_service().cleanup_provisional_task(task.id, "other-command")


def test_cleanup_provisional_task_rejects_accepted_run(storage: Path) -> None:
    workspace = _workspace(storage)
    task = get_workspace_service().create_task(
        workspace.id,
        "new task",
        creation_command_id="creation-1",
    )
    get_conversation_run_crud().create(task.id, "accepted", status="pending")

    with pytest.raises(ProvisionalTaskCleanupConflictError):
        get_task_service().cleanup_provisional_task(task.id, "creation-1")
