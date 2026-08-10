"""Regression tests for RuntimeContext message accumulation.

Guards against an infinite-loop defect introduced by the "persist one message at
a time" refactor:

ReactWorkflow._model_node reads model context via
_runtime_context().load_message(). Before the refactor messages were flushed
to DB only at turn end; after the refactor nodes flush per message but must
also write them back to RuntimeContext, otherwise load_message() returns
the same initial snapshot every step and the model loops on identical output.

This file locks two invariants:
1. add_message followed by load_message accumulates across steps (no loop);
2. load_for_task loads history from DB without duplicating or dropping the
   user message flushed by the runner.

Isolation: each case uses tmp_path as an independent main DB. Teardown must
call reset_service_dependencies to clear the get_turn_message_crud
lru_cache, otherwise the CRUD singleton (bound to the old engine's session
factory) leaks across cases and writes fail on FK constraints.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock

import sqlalchemy
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.config.settings import Settings
from app.core.agents.agent_profile import AgentProfile
from app.core.context.runtime_context import RuntimeContext
from app.core.runtime.runtime_operations import RuntimeOperations
from app.core.workflows.nodes import tools_node as tn
from app.core.workflows.nodes.tools_node import _persist_tool_observations
from app.core.workflows.react.state import ReactGraphState
from app.models.runtime_message import RuntimeMessage
from app.service.depends import reset_service_dependencies
from app.service.task.turn_service import TurnService
from app.storage.store_engines import close_storage, init_storage, main_engine


def _setup_storage(tmp_path) -> None:
    """Initialize the main DB in a temp dir (tmp_path is already unique)."""

    Settings.override(
        DATABASE_FILE=tmp_path / "app.db",
        LOG_DATABASE_FILE=tmp_path / "log.db",
        CHECKPOINT_FILE=tmp_path / "checkpoint.db",
    )
    init_storage()


def _teardown_storage() -> None:
    """Release engines and clear service-layer cached singletons."""

    close_storage()
    reset_service_dependencies()
    Settings.override(DATABASE_FILE=None, LOG_DATABASE_FILE=None, CHECKPOINT_FILE=None)


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
                "VALUES (:tid, :kid, 'i', 'pending', "
                "'2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')"
            ),
            {"tid": turn_id, "kid": task_id},
        )


def _make_operations(turn_id: str) -> RuntimeOperations:
    """Construct a lightweight RuntimeOperations with a real TurnService facade."""

    ops = RuntimeOperations.__new__(RuntimeOperations)
    ops._current_turn = SimpleNamespace(turn_id=turn_id)
    ops._message_sequence = 0
    ops._turn_service = TurnService()
    return ops


def _make_profile() -> AgentProfile:
    """Construct a minimal test AgentProfile with required context_policy."""

    return AgentProfile(
        agent_id="developer",
        role="developer",
        goal="assist with software engineering tasks",
        allowed_tools=[],
        context_policy="default",
    )


def _make_context() -> RuntimeContext:
    """Construct an empty runtime context without triggering history load."""

    return RuntimeContext(
        task_id="t", agent_profile=_make_profile(), workspace_root="x"
    )


def test_add_message_accumulates_across_steps(tmp_path):
    """Each step's add_message grows load_message, preventing a stale-snapshot loop."""

    _setup_storage(tmp_path)
    try:
        _seed_turn("task-acc", "turn-acc")
        ops = _make_operations("turn-acc")
        rc = _make_context()

        # step 1: model output assistant, node flushes + write-back
        # load_message has 1 auto-injected system prompt + 1 assistant
        ai_1 = AIMessage(
            content="step1",
            tool_calls=[{"name": "read_file", "args": {"path": "a"}, "id": "c1"}],
        )
        ops.append_runtime_message(RuntimeMessage(role="assistant", content_text="step1"))
        rc.add_message(ai_1)
        assert len(rc.load_message()) == 2

        # step 2: tool output observation, node flushes + write-back
        # accumulates to system prompt + assistant + tool = 3
        tool_obs = ToolMessage(content="content-a", tool_call_id="c1")
        ops.append_runtime_message(
            RuntimeMessage(
                role="tool", content_text="content-a", metadata={"tool_call_id": "c1"}
            )
        )
        rc.add_message(tool_obs)
        assert len(rc.load_message()) == 3

        # step 3: model reads again and sees the 3 accumulated messages
        assert len(rc.load_message()) == 3
        roles = [type(m).__name__ for m in rc.load_message()]
        assert roles == ["SystemMessage", "AIMessage", "ToolMessage"]
    finally:
        _teardown_storage()


def test_load_for_task_no_duplicate_user(tmp_path):
    """The runner-flushed user message loads exactly once via load_for_task."""

    _setup_storage(tmp_path)
    try:
        _seed_turn("task-user", "turn-user")
        ops = _make_operations("turn-user")
        ops.append_runtime_message(RuntimeMessage(role="user", content_text="hello"))

        ctx = RuntimeContext.load_for_task(
            agent_profile=_make_profile(),
            workspace_root="x",
            task_id="task-user",
        )
        user_msgs = [m for m in ctx.messages if isinstance(m, HumanMessage)]
        assert len(user_msgs) == 1
        assert user_msgs[0].content == "hello"
    finally:
        _teardown_storage()


def test_persist_tool_observations_closes_tool_call_pair(tmp_path):
    """_persist_tool_observations flushes + write-backs so AIMessage.tool_calls pair up.

    Simulates _model_node having written back an AIMessage with tool_calls, then
    _persist_tool_observations writing back the ToolMessage: load_message() should
    hold both, with AIMessage.tool_calls[0].id matching ToolMessage.tool_call_id.
    """

    _setup_storage(tmp_path)
    try:
        _seed_turn("task-pair", "turn-pair")
        ops = _make_operations("turn-pair")
        rc = _make_context()

        ai_1 = AIMessage(
            content="step1",
            tool_calls=[{"name": "read_file", "args": {"path": "a"}, "id": "c1"}],
        )
        ops.append_runtime_message(RuntimeMessage(role="assistant", content_text="step1"))
        rc.add_message(ai_1)

        obs = RuntimeMessage(
            role="tool",
            content_text="content-a",
            metadata={"tool_call_id": "c1"},
        )
        with mock.patch.object(tn, "_runtime_context", return_value=rc):
            _persist_tool_observations(ops, [obs])

        msgs = rc.load_message()
        assert isinstance(msgs[1], AIMessage)
        assert msgs[1].tool_calls[0]["id"] == "c1"
        assert isinstance(msgs[2], ToolMessage)
        assert msgs[2].tool_call_id == "c1"
        assert ops._message_sequence == 2
    finally:
        _teardown_storage()


def test_persist_tool_observations_no_orphan_strip(tmp_path):
    """Write-back does not strip dangling tool_calls from the AIMessage.

    _persist_tool_observations only writes back the passed obs; it does not strip
    dangling tool_calls like runtime_to_langchain would. Missing pairing is closed
    by the _tools_node cancel-branch placeholder logic, not by silently dropping.
    """

    _setup_storage(tmp_path)
    try:
        _seed_turn("task-orphan", "turn-orphan")
        ops = _make_operations("turn-orphan")
        rc = _make_context()
        ai_1 = AIMessage(
            content="step1",
            tool_calls=[{"name": "read_file", "args": {"path": "a"}, "id": "c1"}],
        )
        rc.add_message(ai_1)
        with mock.patch.object(tn, "_runtime_context", return_value=rc):
            _persist_tool_observations(
                ops,
                [RuntimeMessage(role="tool", content_text="x", metadata={"tool_call_id": "c2"})],
            )
            ai_in_ctx = next(m for m in rc.load_message() if isinstance(m, AIMessage))
            assert any(call["id"] == "c1" for call in ai_in_ctx.tool_calls)
    finally:
        _teardown_storage()


def _make_state() -> ReactGraphState:
    """Build a minimal graph state fixture for the cancel branch."""

    return ReactGraphState(
        step_count=1,
        tool_error_count=0,
        requested_tool=True,
        final_response=False,
        terminal=False,
        pending_tool_calls=[
            {"tool_name": "read_file", "arguments": {"path": "a"}, "call_id": "c1"},
        ],
        max_steps=10,
        final_text="",
    )


def test_tools_node_cancel_branch_returns_basemessage(tmp_path):
    """_tools_node cancel branch returns BaseMessage (not RuntimeMessage).

    When the turn is cancelled, the branch skips tool execution, appends a
    placeholder ToolMessage to the context, and returns the delta. Its messages
    field must be a list[BaseMessage] (via runtime_to_langchain), consistent
    with other branches, so LangGraph's add_messages reducer does not choke.
    """

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
        with mock.patch.object(tn, "_runtime_config", return_value=cfg), \
                mock.patch.object(tn, "_runtime_context", return_value=rc), \
                mock.patch.object(ops, "is_current_turn_cancelled", return_value=True), \
                mock.patch.object(tn, "_persist_tool_observations") as persist_mock:

            state = _make_state()
            result = asyncio.run(tn._tools_node(state))

            persist_mock.assert_called_once()
            messages = result["messages"]
            assert isinstance(messages, list)
            assert len(messages) == 1
            assert isinstance(messages[0], ToolMessage)
            assert messages[0].tool_call_id == "c1"
            assert result["terminal"] is True
            assert result["pending_tool_calls"] == []
    finally:
        _teardown_storage()
