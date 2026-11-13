"""Task metadata is typed once at storage; four kinds retain their distinct ownership."""

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.api.schemas.response.TaskResponse import TaskResponse
from app.core.workflow.message_queue.message_bodies.user_input import RunLaunch
from app.models.task_extra import TaskExtra, TaskFork
from app.models.task_kind import TaskType
from app.models.task_record import TaskRecord
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.model.task_model import TaskModel
from app.storage.store_engines import main_session_factory
from tests.task_extra_fixtures import fork_extra


@pytest.mark.usefixtures("isolated_storage")
@pytest.mark.parametrize("kind", list(TaskType))
def test_task_kind_and_typed_extra_round_trip(kind, tmp_path):
    workspace = WorkspaceCrud().create("types", str(tmp_path))
    extra = fork_extra(123) if kind in {TaskType.USER_FORK, TaskType.AGENT_FORK} else TaskExtra()
    task = TaskCrud().create(workspace.id, "task", task_type=kind, extra=extra)
    loaded = TaskCrud().get(task.id)
    assert loaded.task_type is kind and isinstance(loaded.extra, TaskExtra)
    assert loaded.extra == extra
    assert TaskRecord.from_model(loaded.to_model()).extra == extra
    assert loaded.to_dict()["extra"] == extra.model_dump(mode="json")
    with main_session_factory()() as session:
        raw = session.scalar(select(TaskModel.extra).where(TaskModel.id == task.id))
    assert isinstance(raw, dict) and "created_by" not in str(raw)
    response = TaskResponse.from_record(loaded).model_dump(mode="json")
    assert response["task_type"] == kind.value
    if extra.fork is not None:
        assert response["extra"]["fork"] == {"tree_parent_task_id": 123}
        assert RunLaunch.from_fork(loaded) is None  # absent model does not prevent history fork
        assert loaded.extra.fork.initial_launch.options.reasoning_effort == "low"


@pytest.mark.parametrize(
    "kind,extra",
    [
        (TaskType.USER, fork_extra(1)),
        (TaskType.USER_FORK, TaskExtra()),
        (TaskType.AGENT_FORK, TaskExtra()),
        (TaskType.AGENT_TASK, fork_extra(1)),
        (TaskType.USER, TaskExtra(deriving_call_id="call-1")),
    ],
)
def test_task_metadata_must_match_its_type(kind, extra):
    with pytest.raises(ValueError):
        extra.validate_task_type(kind)


def test_unknown_metadata_and_duplicate_creator_are_rejected():
    with pytest.raises(ValidationError):
        TaskExtra.model_validate({"arbitrary": 1})
    data = fork_extra(1).fork.model_dump(mode="json")
    for removed in ("created_by", "tool_call_id"):
        with pytest.raises(ValidationError):
            TaskFork.model_validate({**data, removed: "obsolete"})


@pytest.mark.usefixtures("isolated_storage")
def test_reparent_changes_only_tree_relationship(tmp_path):
    workspace = WorkspaceCrud().create("tree", str(tmp_path))
    crud = TaskCrud()
    child = crud.create(workspace.id, "child", task_type=TaskType.AGENT_FORK, extra=fork_extra(123))
    with main_session_factory().begin() as session:
        assert crud.reparent_forks(workspace.id, frozenset({123}), None, session) == 1
    after = crud.get(child.id)
    assert after.extra.fork.tree_parent_task_id is None
    assert after.extra.fork.source_task_id == 123 and after.deriver_id() == 123
    assert after.extra.fork.initial_launch == child.extra.fork.initial_launch
