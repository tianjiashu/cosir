"""run 级联删除（TaskService.delete_run 与 DELETE /tasks/{id}/runs/{id}）行为测试。"""

from pathlib import Path

import pytest
from fastapi import HTTPException
from langchain_core.messages import HumanMessage
from sqlalchemy import func, select

from app.api.tasks_api import delete_run as delete_run_endpoint
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.errors.deletion_errors import DeletionBusyError, RunDeletionConflictError
from app.service.depends import (
    close_service_dependencies,
    get_conversation_command_crud,
    get_conversation_run_crud,
    get_conversation_task_context_crud,
    get_task_crud,
    get_task_service,
    get_workspace_crud,
)
from app.storage.model.conversation_run_model import ConversationRunModel
from app.storage.model.conversation_task_context_model import ConversationTaskContextModel
from app.storage.model.delegation_model import DelegationModel
from app.storage.store_engines import init_storage, main_session_factory
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.utils import paths


@pytest.fixture
def storage(tmp_path: Path):
    """为 run 删除测试提供隔离的主库和 checkpoint 路径。"""

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


def _count(model: type[object]) -> int:
    with main_session_factory()() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _insert_delegation(task_id: int, parent_run_id: int) -> None:
    """插入一条以 parent_run_id 为发起方的委派记录。"""

    with main_session_factory().begin() as session:
        session.add(
            DelegationModel(
                task_id=task_id,
                parent_run_id=parent_run_id,
                child_run_id=None,
                child_task_id=None,
                parent_agent_id="main_agent",
                child_agent_id="sub_agent",
                status="succeeded",
                prompt="delegate",
                summary="done",
                error="",
                effective_tools="[]",
            )
        )


def test_delete_middle_run_removes_only_its_data(storage) -> None:
    workspace = get_workspace_crud().create("review", str(Path.cwd()))
    task = get_task_crud().create(workspace.id, "root")
    context_crud = get_conversation_task_context_crud()

    run_a = get_conversation_run_crud().create(task.id, "a", status="completed")
    run_b = get_conversation_run_crud().create(task.id, "b", status="completed")
    run_c = get_conversation_run_crud().create(task.id, "c", status="completed")
    get_task_crud().set_current_run_id(task.id, run_c.id)

    for seq, run in ((1, run_a), (2, run_b), (3, run_c)):
        context_crud.create(
            ConversationTaskContextRecord(
                task_id=task.id,
                run_id=run.id,
                message=HumanMessage(content=f"message-{seq}"),
                include_in_context=True,
                sequence=seq,
            )
        )

    # 只给中间 run_b 挂命令 / 委派产物。
    get_conversation_command_crud().create(
        task_id=task.id,
        command_id="cmd-b",
        command_type="new",
        payload_hash="hash-b",
        run_id=run_b.id,
    )
    _insert_delegation(task.id, run_b.id)

    get_task_service().delete_run(task.id, run_b.id)

    # 目标 run 及其上下文被删除，兄弟 run 与上下文保留。
    with main_session_factory()() as session:
        remaining_run_ids = set(
            session.scalars(
                select(ConversationRunModel.id).where(ConversationRunModel.task_id == task.id)
            ).all()
        )
        seq_by_run = dict(
            session.execute(
                select(ConversationTaskContextModel.run_id, ConversationTaskContextModel.sequence)
            ).all()
        )
    assert remaining_run_ids == {run_a.id, run_c.id}
    assert seq_by_run.get(run_b.id) is None
    # 序号不重排：run_c 仍保留原 sequence=3。
    assert seq_by_run[run_c.id] == 3
    assert seq_by_run[run_a.id] == 1

    with main_session_factory()() as session:
        remaining_context = int(
            session.scalar(
                select(func.count())
                .select_from(ConversationTaskContextModel)
                .where(ConversationTaskContextModel.run_id == run_b.id)
            )
            or 0
        )
    assert remaining_context == 0

    # current_run_id 指向未删除的 run_c，保持不变。
    assert get_task_crud().get(task.id).current_run_id == run_c.id


def test_delete_run_rejected_when_task_has_active_run(storage) -> None:
    workspace = get_workspace_crud().create("review", str(Path.cwd()))
    task = get_task_crud().create(workspace.id, "root")
    done_run = get_conversation_run_crud().create(task.id, "done", status="completed")
    get_conversation_run_crud().create(task.id, "running", status="running")

    with pytest.raises(RunDeletionConflictError):
        get_task_service().delete_run(task.id, done_run.id)

    # 守卫拒绝后不应删除任何 run。
    assert _count(ConversationRunModel) == 2


def test_delete_run_missing_or_foreign_run_raises_key_error(storage) -> None:
    workspace = get_workspace_crud().create("review", str(Path.cwd()))
    task = get_task_crud().create(workspace.id, "root")
    other_task = get_task_crud().create(workspace.id, "other")
    foreign_run = get_conversation_run_crud().create(other_task.id, "foreign", status="completed")

    with pytest.raises(KeyError):
        get_task_service().delete_run(task.id, 987654)
    # run 存在但不属于该 task。
    with pytest.raises(KeyError):
        get_task_service().delete_run(task.id, foreign_run.id)


@pytest.mark.asyncio
async def test_delete_run_endpoint_maps_errors() -> None:
    class MissingRun:
        def delete_run(self, _task_id: int, _run_id: int) -> None:
            raise KeyError(_run_id)

    class ConflictRun:
        def delete_run(self, _task_id: int, _run_id: int) -> None:
            raise RunDeletionConflictError("TASK_HAS_ACTIVE_RUN", "active run")

    class BusyRun:
        def delete_run(self, _task_id: int, _run_id: int) -> None:
            raise DeletionBusyError("task", _task_id)

    with pytest.raises(HTTPException) as not_found:
        await delete_run_endpoint(1, 2, MissingRun())
    assert not_found.value.status_code == 404

    with pytest.raises(HTTPException) as conflict:
        await delete_run_endpoint(1, 2, ConflictRun())
    assert conflict.value.status_code == 409
    assert conflict.value.detail["code"] == "TASK_HAS_ACTIVE_RUN"

    with pytest.raises(HTTPException) as busy:
        await delete_run_endpoint(1, 2, BusyRun())
    assert busy.value.status_code == 409
    assert busy.value.detail["code"] == "TASK_BUSY"
