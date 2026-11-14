"""Session metadata is typed once at storage; four kinds retain their distinct ownership."""

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.api.schemas.response.SessionResponse import SessionResponse
from app.core.workflow.message_queue.message_bodies.user_input import RunLaunch
from app.models.session_extra import SessionExtra, SessionFork
from app.models.session_kind import SessionType
from app.models.session_record import SessionRecord
from app.storage.crud.session_crud import SessionCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.model.session_model import SessionModel
from app.storage.store_engines import main_db_factory
from tests.session_extra_fixtures import fork_extra


@pytest.mark.usefixtures("isolated_storage")
@pytest.mark.parametrize("kind", list(SessionType))
def test_session_kind_and_typed_extra_round_trip(kind, tmp_path):
    workspace = WorkspaceCrud().create("types", str(tmp_path))
    extra = fork_extra(123) if kind in {SessionType.USER_FORK, SessionType.AGENT_FORK} else SessionExtra()
    session = SessionCrud().create(workspace.id, "session", session_type=kind, extra=extra)
    loaded = SessionCrud().get(session.id)
    assert loaded.session_type is kind and isinstance(loaded.extra, SessionExtra)
    assert loaded.extra == extra
    assert SessionRecord.from_model(loaded.to_model()).extra == extra
    assert loaded.to_dict()["extra"] == extra.model_dump(mode="json")
    with main_db_factory()() as db:
        raw = db.scalar(select(SessionModel.extra).where(SessionModel.id == session.id))
    assert isinstance(raw, dict) and "created_by" not in str(raw)
    response = SessionResponse.from_record(loaded).model_dump(mode="json")
    assert response["session_type"] == kind.value
    if extra.fork is not None:
        assert response["extra"]["fork"] == {"tree_parent_session_id": 123}
        assert RunLaunch.from_fork(loaded) is None  # absent model does not prevent history fork
        assert loaded.extra.fork.initial_launch.options.reasoning_effort == "low"


@pytest.mark.parametrize(
    "kind,extra",
    [
        (SessionType.USER, fork_extra(1)),
        (SessionType.USER_FORK, SessionExtra()),
        (SessionType.AGENT_FORK, SessionExtra()),
        (SessionType.DELEGATED, fork_extra(1)),
        (SessionType.USER, SessionExtra(deriving_call_id="call-1")),
    ],
)
def test_session_metadata_must_match_its_type(kind, extra):
    with pytest.raises(ValueError):
        extra.validate_session_type(kind)


def test_unknown_metadata_and_duplicate_creator_are_rejected():
    with pytest.raises(ValidationError):
        SessionExtra.model_validate({"arbitrary": 1})
    data = fork_extra(1).fork.model_dump(mode="json")
    for removed in ("created_by", "tool_call_id"):
        with pytest.raises(ValidationError):
            SessionFork.model_validate({**data, removed: "obsolete"})


@pytest.mark.usefixtures("isolated_storage")
def test_reparent_changes_only_tree_relationship(tmp_path):
    workspace = WorkspaceCrud().create("tree", str(tmp_path))
    crud = SessionCrud()
    child = crud.create(workspace.id, "child", session_type=SessionType.AGENT_FORK, extra=fork_extra(123))
    with main_db_factory().begin() as db:
        assert crud.reparent_forks(workspace.id, frozenset({123}), None, db) == 1
    after = crud.get(child.id)
    assert after.extra.fork.tree_parent_session_id is None
    assert after.extra.fork.source_session_id == 123 and after.deriver_id() == 123
    assert after.extra.fork.initial_launch == child.extra.fork.initial_launch
