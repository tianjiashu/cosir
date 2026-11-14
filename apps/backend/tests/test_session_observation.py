"""会话观测：Run 的状态迁移历史只在迁移命中时追加，观测结果按回合裁剪运行节点。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.api.schemas.response.SessionObservationResponse import SessionObservationResponse
from app.models.enums.run_status import RunStatus
from app.models.message_metadata import ToolMessageMetadata, UserMessageMetadata
from app.models.run_extra import RunExtra, RunStatusChange
from app.models.run_record import RunRecord
from app.models.session_context import SessionContextRecord
from app.service.conversation.session_context_service import SessionContextService
from app.service.conversation.session_observation import SessionObservation
from app.storage.crud.run_crud import RunCrud
from app.storage.crud.session_crud import SessionCrud
from app.storage.crud.workspace_crud import WorkspaceCrud

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _at(seconds: int) -> str:
    return (_T0 + timedelta(seconds=seconds)).isoformat()


@pytest.mark.usefixtures("isolated_storage")
def test_status_history_records_only_applied_transitions(tmp_path) -> None:
    workspace = WorkspaceCrud().create("observation", str(tmp_path))
    session = SessionCrud().create(workspace.id, "session")
    crud = RunCrud()
    run = crud.create(session.id, RunStatus.RUNNING.value)
    assert [c.status for c in run.extra.status_history] == ["running"]
    assert run.extra.status_history[0].at == run.created_at.isoformat()

    suspended = crud.update_status_if_in(run.id, "suspended", ("running",))
    assert suspended is not None
    # 来源状态不符：不改状态，也不留历史。
    assert crud.update_status_if_in(run.id, "suspended", ("running",)) is None
    crud.update_status_if_in(run.id, "running", ("suspended",))
    done = crud.update_status_if_in(run.id, "completed", ("running",), end_reason="stop")

    assert done is not None
    history = crud.get(run.id).extra.status_history
    assert [(c.status, c.end_reason) for c in history] == [
        ("running", None),
        ("suspended", None),
        ("running", None),
        ("completed", "stop"),
    ]
    assert history[-1].at == done.updated_at.isoformat()


@pytest.mark.usefixtures("isolated_storage")
def test_service_reads_title_and_run_nodes_from_storage(tmp_path) -> None:
    workspace = WorkspaceCrud().create("observation", str(tmp_path))
    session = SessionCrud().create(workspace.id, "排查登录失败")
    run = RunCrud().create(session.id, RunStatus.RUNNING.value)
    RunCrud().update_status_if_in(run.id, "completed", ("running",), end_reason="stop")

    response = SessionObservationResponse.from_observation(
        SessionContextService().observe_session(session.id)
    )

    assert (response.session_title, response.last_turn) == ("排查登录失败", 0)
    assert [(c.status, c.latest) for c in response.runs[0].changes] == [
        ("running", False),
        ("completed", True),
    ]
    with pytest.raises(KeyError):
        SessionContextService().observe_session(session.id + 1)


def _run(run_id: int, changes: list[tuple[int, str]]) -> RunRecord:
    return RunRecord(
        id=run_id,
        session_id=1,
        status=changes[-1][1],
        created_at=_T0 + timedelta(seconds=changes[0][0]),
        updated_at=_T0 + timedelta(seconds=changes[-1][0]),
        extra=RunExtra(
            status_history=[RunStatusChange(at=_at(at), status=status) for at, status in changes]
        ),
    )


def _row(sequence: int, turn: int, at: int, message: dict, **fields) -> SessionContextRecord:
    return SessionContextRecord(
        session_id=1,
        run_id=fields.pop("run_id", 1),
        sequence=sequence,
        turn=turn,
        include_in_context=fields.pop("include_in_context", True),
        message=message,
        created_at=_at(at),
        updated_at=_at(at),
        **fields,
    )


def _user(kind: str, **metadata) -> UserMessageMetadata:
    return UserMessageMetadata.model_validate({"kind": kind, **metadata})


_CALL = {"id": "call_1", "type": "function", "function": {"name": "ask_user", "arguments": "{}"}}


def _rows() -> list[SessionContextRecord]:
    """一个 Run、两个回合：回合 1 发起提问后挂起，被开启回合 2 的输入唤醒。"""
    return [
        _row(1, 1, 1, {"role": "assistant", "content": "", "tool_calls": [_CALL]}),
        _row(
            2,
            1,
            2,
            {"role": "tool", "tool_call_id": "call_1", "content": "等待"},
            metadata=ToolMessageMetadata(status="cancelled"),
        ),
        _row(
            3,
            2,
            11,
            {"role": "user", "content": "<system_notice>…</system_notice>"},
            metadata=_user("notice"),
            project_to_transport=False,
        ),
        _row(
            4,
            2,
            12,
            {"role": "assistant", "content": "被撤回"},
            include_in_context=False,
            project_to_transport=False,
        ),
    ]


_RUN_CHANGES = [(0, "running"), (3, "suspended"), (10, "running"), (20, "completed")]


def test_session_wide_observation_keeps_every_row_and_node() -> None:
    observation = SessionObservation.build("标题", _rows(), [_run(1, _RUN_CHANGES)], None)
    response = SessionObservationResponse.from_observation(observation)

    assert (response.session_title, response.last_turn) == ("标题", 2)
    assert [m.sequence for m in response.messages] == [1, 2, 3, 4]
    tool, notice, withdrawn = response.messages[1:]
    assert (tool.tool_name, tool.status) == ("ask_user", "cancelled")
    assert (notice.kind, notice.project_to_transport, notice.withdrawn) == ("notice", False, False)
    assert (withdrawn.include_in_context, withdrawn.withdrawn) == (False, True)
    assert [(c.previous_status, c.status) for c in response.runs[0].changes] == [
        (None, "running"),
        ("running", "suspended"),
        ("suspended", "running"),
        ("running", "completed"),
    ]


def test_turn_observation_clips_nodes_to_the_turn_window() -> None:
    runs = [_run(1, _RUN_CHANGES)]

    first = SessionObservation.build("标题", _rows(), runs, 1)
    assert [row.sequence for row in first.rows] == [1, 2]
    # Run 的第一个回合不设下界（「开始」早于第一行）；唤醒先于回合 2 的第一行，归在这里。
    assert [(c.previous_status, c.status) for c in first.runs[0].changes] == [
        (None, "running"),
        ("running", "suspended"),
        ("suspended", "running"),
    ]
    # 裁剪只动节点，工具名仍取自整个 Session。
    assert first.tool_names == {"call_1": "ask_user"}

    second = SessionObservation.build("标题", _rows(), runs, 2)
    assert second.last_turn == 2
    # 裁掉的上一个节点仍给出来源与时刻，前端据此算「运行了多久」。
    (end,) = second.runs[0].changes
    assert (end.previous_status, end.previous_at, end.status, end.latest) == (
        "running",
        _at(10),
        "completed",
        True,
    )


def test_inherited_turn_has_no_run() -> None:
    rows = [_row(1, 1, 1, {"role": "assistant", "content": "继承"}, run_id=None)]
    assert SessionObservation.build("标题", rows, [], 1).runs == []
