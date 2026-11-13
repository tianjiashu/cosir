"""验证 provisional Session 的创建标记、受控清理与附件保留边界。"""

from pathlib import Path

import pytest

from app.models.errors.deletion_errors import ProvisionalSessionCleanupConflictError
from app.service.depends import (
    close_service_dependencies,
    get_run_crud,
    get_session_crud,
    get_session_service,
    get_workspace_service,
)
from app.storage.store_engines import init_storage
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces
from app.utils.path import system_cosir as paths


@pytest.fixture
def storage(tmp_path: Path, install_process_singletons):
    """为 provisional Session 测试提供隔离数据库和附件目录，并装配进程级单例。

    Session 清理链路会读 agent 目录（未装配即 ``RuntimeError``）；单例必须在主库初始化之后安装。
    """

    close_service_dependencies()
    db_dir = tmp_path / "storage"
    db_dir.mkdir()
    paths.override(
        DATABASE_FILE=db_dir / "app.sqlite3",
        LOG_DIR=db_dir / "logs",
    )
    init_storage()
    install_process_singletons()
    session_runtime_spaces.close()
    yield tmp_path
    session_runtime_spaces.close()
    close_service_dependencies()
    paths.reset()


def _workspace(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    return get_workspace_service().create_workspace("workspace", str(root))


def test_create_session_preserves_creation_command_id(storage: Path) -> None:
    workspace = _workspace(storage)

    session = get_workspace_service().create_session(
        workspace.id,
        "new session",
        creation_command_id="creation-1",
    )

    assert get_session_crud().get(session.id).creation_command_id == "creation-1"


def test_cleanup_provisional_session_keeps_uploaded_attachment(
    storage: Path,
) -> None:
    workspace = _workspace(storage)
    session = get_workspace_service().create_session(
        workspace.id,
        "new session",
        creation_command_id="creation-1",
    )
    attachment = Path(workspace.root_path) / ".cosir" / "Attachment" / "image.png"
    attachment.write_bytes(b"image")
    service = get_session_service()

    service.cleanup_provisional_session(session.id, "creation-1")

    with pytest.raises(KeyError):
        get_session_crud().get(session.id)
    assert attachment.exists()


def test_cleanup_provisional_session_rejects_mismatched_marker(storage: Path) -> None:
    workspace = _workspace(storage)
    session = get_workspace_service().create_session(
        workspace.id,
        "new session",
        creation_command_id="creation-1",
    )

    with pytest.raises(ProvisionalSessionCleanupConflictError):
        get_session_service().cleanup_provisional_session(session.id, "other-command")


def test_cleanup_provisional_session_rejects_accepted_run(storage: Path) -> None:
    workspace = _workspace(storage)
    session = get_workspace_service().create_session(
        workspace.id,
        "new session",
        creation_command_id="creation-1",
    )
    get_run_crud().create(session.id, status="running")

    with pytest.raises(ProvisionalSessionCleanupConflictError):
        get_session_service().cleanup_provisional_session(session.id, "creation-1")


async def test_cleanup_provisional_session_stops_the_actor_it_materialized(
    storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清理为取 Session 闸门物化了 space、拉起了 actor；删除提交后 actor 必须停下，不留挂在已删除
    Session 队列上的 loop。"""
    import asyncio

    from app.core.session_actor import SessionActor

    started: list[SessionActor] = []
    original_start = SessionActor.start

    def record_start(actor: SessionActor) -> None:
        started.append(actor)
        original_start(actor)

    monkeypatch.setattr(SessionActor, "start", record_start)
    session_runtime_spaces.bind_loop(asyncio.get_running_loop())
    workspace = _workspace(storage)
    session = get_workspace_service().create_session(
        workspace.id, "new session", creation_command_id="creation-1"
    )

    await asyncio.to_thread(get_session_service().cleanup_provisional_session, session.id, "creation-1")

    assert session_runtime_spaces.get(session.id) is None
    [actor] = [actor for actor in started if actor.session_id == session.id]
    async with asyncio.timeout(2):
        await actor.stopped.wait()
