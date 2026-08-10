"""Boundary tests for the incremental persistence refactor.

Covers edge cases for _persist_tool_observations / RuntimeContext /
append_runtime_message:
- empty batch: no DB write, no context write-back, no exception
- DB write failure: exception propagates, context is not polluted
- load_message auto-injects the system prompt
- repeated add_message keeps stable order
- cancel branch returns a BaseMessage (also covered in the accumulation test)
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock

import pytest
import sqlalchemy
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from sqlalchemy.exc import SQLAlchemyError

from app.config.settings import Settings
from app.core.context.runtime_context import RuntimeContext
from app.core.runtime.runtime_operations import RuntimeOperations
from app.core.workflows.nodes import tools_node as tn
from app.core.workflows.react.state import ReactGraphState
from app.models.runtime_message import RuntimeMessage
from app.service.depends import reset_service_dependencies
from app.service.task.turn_service import TurnService
from app.storage.store_engines import close_storage, init_storage, main_engine


def _make_profile() -> object:
    return SimpleNamespace(
        agent_id="dev",
        role="developer",
        goal="",
        model_name="deepseek",
        allowed_tools=[],
        context_policy="default",
    )


def _make_context() -> RuntimeContext:
    return RuntimeContext(
        task_id="task-boundary",
        agent_profile=_make_profile(),
        workspace_root="H:/coding-agent/apps/backend/temp",
    )


def _setup_storage(tmp_path):
    Settings.override(
        DATABASE_FILE=tmp_path / "app.db",
        LOG_DATABASE_FILE=tmp_path / "log.db",
        CHECKPOINT_FILE=tmp_path / "checkpoint.db",
    )
    init_storage()


def _seed_turn(task_id: str, turn_id: str) -> None:
    """Insert minimal workspace->task->turn reference rows for FK constraints."""

    with main_engine().begin() as conn:
        conn.execute(
            sqlalchemy.text(
                "INSERT OR IGNORE INTO workspaces(workspace_id, name, root_path, "
                "created_at, updated_at) "
                "VALUES ('ws-1', 'ws', 'x', '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')"
            )
        )
        conn.execute(
            sqlalchemy.text(
                "INSERT OR IGNORE INTO tasks(task_id, workspace_id, agent_id, input_text, "
                "title, last_message_preview, status, created_at, updated_at) "
                "VALUES (:kid, 'ws-1', 'developer', 'i', 't', 'p', 'active', "
                "'2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')"
            ),
            {"kid": task_id},
        )
        conn.execute(
            sqlalchemy.text(
                "INSERT OR IGNORE INTO turns(turn_id, task_id, input_text, status, "
                "created_at, updated_at) "
                "VALUES (:tid, :kid, 'hello', 'pending', "
                "'2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')"
            ),
            {"tid": turn_id, "kid": task_id},
        )


def _make_operations(turn_id: str) -> RuntimeOperations:
    turn_service = TurnService()
    ops = RuntimeOperations.__new__(RuntimeOperations)
    ops._current_turn = SimpleNamespace(turn_id=turn_id)
    ops._message_sequence = 0
    ops._turn_service = turn_service
    return ops


def _teardown_storage() -> None:
    close_storage()
    reset_service_dependencies()
    Settings.override(DATABASE_FILE=None, LOG_DATABASE_FILE=None, CHECKPOINT_FILE=None)


def test_persist_empty_batch_noop(tmp_path):
    """Empty batch: no DB write, no context write-back, no exception."""

    _setup_storage(tmp_path)
    try:
        ops = _make_operations("turn-empty")
        rc = _make_context()
        with mock.patch.object(tn, "_runtime_context", return_value=rc), \
                mock.patch.object(ops, "append_runtime_message") as append_mock:
            tn._persist_tool_observations(ops, [])
            append_mock.assert_not_called()
            # only the auto-injected system prompt remains (construction-time)
            assert len(rc.messages) == 1
    finally:
        _teardown_storage()


def test_append_failure_propagates_and_skips_context(tmp_path):
    """DB write failure: exception propagates, context is not polluted."""

    _setup_storage(tmp_path)
    try:
        _seed_turn("task-append-fail", "turn-append-fail")
        ops = _make_operations("turn-append-fail")
        rc = _make_context()
        with mock.patch.object(tn, "_runtime_context", return_value=rc), \
                mock.patch.object(
                    ops, "append_runtime_message", side_effect=SQLAlchemyError("db down")
                ):
            obs = [RuntimeMessage(role="tool", content_text="x", metadata={"tool_call_id": "c1"})]
            with pytest.raises(SQLAlchemyError):
                tn._persist_tool_observations(ops, obs)
            assert len(rc.messages) == 1
    finally:
        _teardown_storage()


def test_load_message_injects_system_prompt(tmp_path):
    """load_message auto-injects a SystemMessage as the first message."""

    _setup_storage(tmp_path)
    try:
        _seed_turn("task-sys", "turn-sys")
        ops = _make_operations("turn-sys")
        ctx = RuntimeContext(
            task_id="task-sys",
            agent_profile=_make_profile(),
            workspace_root="H:/coding-agent/apps/backend/temp",
        )
        ops.append_runtime_message(RuntimeMessage(role="user", content_text="hi", metadata={}))
        ctx.add_message(AIMessage(content="hello"))
        messages = ctx.load_message()
        assert isinstance(messages[0], SystemMessage)
        assert len(messages) == 2
    finally:
        _teardown_storage()


def test_repeated_add_message_order_stable(tmp_path):
    """Repeated add_message keeps stable order and does not drop messages."""

    _setup_storage(tmp_path)
    try:
        rc = _make_context()
        for i in range(3):
            rc.add_message(AIMessage(content=f"m{i}"))
        messages = rc.load_message()
        assert isinstance(messages[0], SystemMessage)
        contents = [m.content for m in messages[1:]]
        assert contents == ["m0", "m1", "m2"]
    finally:
        _teardown_storage()


def test_tools_node_cancel_branch_returns_basemessage(tmp_path):
    """Cancel branch returns a BaseMessage (ToolMessage) - contract lock."""

    _setup_storage(tmp_path)
    try:
        _seed_turn("task-cancel", "turn-cancel")
        ops = _make_operations("turn-cancel")
        rc = _make_context()
        cfg = SimpleNamespace(
            operations=ops,
            task=SimpleNamespace(task_id="task-cancel"),
            turn=SimpleNamespace(turn_id="turn-cancel"),
            approval_resolver=None,
        )
        state = ReactGraphState(
            step_count=1,
            tool_error_count=0,
            requested_tool=True,
            final_response=False,
            terminal=False,
            pending_tool_calls=[
                {"tool_name": "read_file", "arguments": {"path": "a"}, "call_id": "c1"}
            ],
            max_steps=10,
            final_text="",
        )
        with mock.patch.object(tn, "_runtime_config", return_value=cfg), \
                mock.patch.object(tn, "_runtime_context", return_value=rc), \
                mock.patch.object(ops, "is_current_turn_cancelled", return_value=True), \
                mock.patch.object(tn, "_persist_tool_observations") as persist_mock:
            result = asyncio.run(tn._tools_node(state))
            persist_mock.assert_called_once()
            messages = result["messages"]
            assert isinstance(messages[0], ToolMessage)
            assert messages[0].tool_call_id == "c1"
            assert result["terminal"] is True
            assert result["pending_tool_calls"] == []
    finally:
        _teardown_storage()
