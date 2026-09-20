"""``conversation_commands`` 按 run 查询的持久化契约测试。

一个 Conversation Run 可以绑定多条命令：``new`` 建 run 时写一条，之后每次「编辑重跑」
（``edit``）都会再写一条并复用同一 ``run_id``。因此「按 run 取命令」必须返回**最近一条**，
不能假设单行。

回归背景：``ConversationCommandCrud.get_by_run`` 曾用 ``scalar_one_or_none()`` 读取，
在多命令 run 上抛 ``MultipleResultsFound``，并让续跑流程把 run 卡成无执行器的 ``running``。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.utils import paths
from app.service.depends import (
    close_service_dependencies,
    get_conversation_command_crud,
    get_conversation_run_crud,
    get_task_crud,
    get_workspace_crud,
)
from app.storage.store_engines import init_storage
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[None]:
    """为命令查询契约测试提供隔离的主库路径。"""

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
    yield
    task_runtime_spaces.close()
    close_service_dependencies()
    paths.reset()


def _new_task() -> int:
    """建一个最小 workspace + task，返回 task 标识。"""

    workspace = get_workspace_crud().create("review", str(Path.cwd()))
    return get_task_crud().create(workspace.id, "root").id


def test_get_by_run_returns_latest_command_for_multi_command_run(storage: None) -> None:
    """同一 run 绑定多条命令时，按 run 查询返回最近一条且不抛 MultipleResultsFound。"""

    task_id = _new_task()
    run = get_conversation_run_crud().create(task_id, "root input", status="cancelled")
    crud = get_conversation_command_crud()
    crud.create(task_id, "transport-first", "add-message", "hash-1", run_id=run.id)
    crud.create(task_id, "transport-second", "add-message", "hash-2", run_id=run.id)

    record = crud.get_by_run(run.id)

    assert record is not None
    assert record.command_id == "transport-second"
    assert record.run_id == run.id


def test_get_by_run_returns_none_without_bound_command(storage: None) -> None:
    """没有任何命令绑定该 run 时返回 None，而不是抛异常。"""

    task_id = _new_task()
    run = get_conversation_run_crud().create(task_id, "root input", status="pending")

    assert get_conversation_command_crud().get_by_run(run.id) is None


def test_get_by_run_in_session_matches_latest_command(storage: None) -> None:
    """事务内读取与自开会话读取返回同一条（最近一次提交）。"""

    task_id = _new_task()
    run = get_conversation_run_crud().create(task_id, "root input", status="cancelled")
    crud = get_conversation_command_crud()
    crud.create(task_id, "transport-first", "add-message", "hash-1", run_id=run.id)
    crud.create(task_id, "transport-second", "add-message", "hash-2", run_id=run.id)

    from app.storage.store_engines import main_session_factory

    with main_session_factory()() as session:
        in_session = crud.get_by_run_in_session(session, run.id)

    assert in_session is not None
    assert in_session.id == crud.get_by_run(run.id).id
