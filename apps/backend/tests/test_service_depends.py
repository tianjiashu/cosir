"""Service dependency singleton tests."""

from __future__ import annotations

from pathlib import Path

from app.config.settings import Settings
from app.service import depends as service_depends
from app.service.log_query_service import LogQueryService
from app.storage.store_engines import close_storage, init_storage


def _init_temp_storage(tmp_path: Path) -> None:
    """Initialize storage against temporary SQLite files.

    参数:
        tmp_path: pytest 提供的临时目录。

    返回:
        无。

    异常:
        OSError: 临时数据库目录无法创建时抛出。

    副作用:
        重置 storage 与 service dependency 缓存，并切换 Settings 路径。
    """

    close_storage()
    service_depends.reset_service_dependencies()
    Settings.override(
        DATABASE_FILE=tmp_path / "app.sqlite3",
        LOG_DATABASE_FILE=tmp_path / "logs.sqlite3",
        CHECKPOINT_FILE=tmp_path / "checkpoint.sqlite3",
    )
    init_storage()


def test_service_depends_reuses_crud_and_store_singletons(tmp_path: Path) -> None:
    """Service dependency accessors must reuse one storage object per process."""

    previous = {
        "DATABASE_FILE": Settings.DATABASE_FILE,
        "LOG_DATABASE_FILE": Settings.LOG_DATABASE_FILE,
        "CHECKPOINT_FILE": Settings.CHECKPOINT_FILE,
    }
    try:
        _init_temp_storage(tmp_path)

        assert service_depends.get_task_crud() is service_depends.get_task_crud()
        assert service_depends.get_turn_crud() is service_depends.get_turn_crud()
        assert service_depends.get_workspace_crud() is service_depends.get_workspace_crud()
        assert service_depends.get_runtime_event_crud() is service_depends.get_runtime_event_crud()
        assert service_depends.get_turn_message_crud() is service_depends.get_turn_message_crud()
        assert service_depends.get_log_store() is service_depends.get_log_store()
    finally:
        close_storage()
        service_depends.reset_service_dependencies()
        Settings.override(**previous)


def test_log_query_service_uses_service_managed_log_store(tmp_path: Path) -> None:
    """LogQueryService must construct without caller-supplied LogStore."""

    previous = {
        "DATABASE_FILE": Settings.DATABASE_FILE,
        "LOG_DATABASE_FILE": Settings.LOG_DATABASE_FILE,
        "CHECKPOINT_FILE": Settings.CHECKPOINT_FILE,
    }
    try:
        _init_temp_storage(tmp_path)
        query_service = LogQueryService()

        result = query_service.recent()

        assert result.entries == []
        assert result.text == ""
    finally:
        close_storage()
        service_depends.reset_service_dependencies()
        Settings.override(**previous)


def test_initialize_service_dependencies_rebuilds_when_paths_change(tmp_path: Path) -> None:
    """Storage initialization must rebuild safely when configured paths change."""

    previous = {
        "DATABASE_FILE": Settings.DATABASE_FILE,
        "LOG_DATABASE_FILE": Settings.LOG_DATABASE_FILE,
        "CHECKPOINT_FILE": Settings.CHECKPOINT_FILE,
    }
    try:
        _init_temp_storage(tmp_path / "first")
        Settings.override(
            DATABASE_FILE=tmp_path / "second" / "app.sqlite3",
            LOG_DATABASE_FILE=tmp_path / "second" / "logs.sqlite3",
            CHECKPOINT_FILE=tmp_path / "second" / "checkpoint.sqlite3",
        )

        service_depends.initialize_service_dependencies()

        assert service_depends.get_task_crud() is service_depends.get_task_crud()
    finally:
        close_storage()
        service_depends.reset_service_dependencies()
        Settings.override(**previous)
