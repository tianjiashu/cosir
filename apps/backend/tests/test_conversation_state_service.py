"""Canonical conversation state projection tests."""

import tempfile
from pathlib import Path

from app.config.settings import Settings
from app.service.task.conversation_mutation_writer import ConversationMutationWriter
from app.service.task.conversation_state_service import ConversationStateService
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.turn_crud import TurnCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.store_engines import close_storage, init_storage


def setup_module() -> None:
    """Initialize an isolated SQLite database for canonical fact tests."""
    root = Path(tempfile.mkdtemp(prefix="cosir-facts-"))
    Settings.override(
        LOG_DIR=root / "logs",
        DATABASE_FILE=root / "app.sqlite3",
        LOG_DATABASE_FILE=root / "logs.sqlite3",
        CHECKPOINT_FILE=root / "checkpoint.sqlite3",
    )
    init_storage()


def teardown_module() -> None:
    """Release the isolated storage engine."""
    close_storage()
    Settings.load()


def _task_id() -> int:
    """Create and return a task aggregate."""
    root = Path(tempfile.mkdtemp(prefix="cosir-workspace-"))
    workspace = WorkspaceCrud().create("facts", str(root))
    return TaskCrud().create(workspace.id, "canonical").id


def test_projection_reads_canonical_messages_and_revision() -> None:
    """State is reconstructed from persisted messages and the committed revision."""
    task_id = _task_id()
    writer = ConversationMutationWriter()
    user, _ = writer.create_message(task_id, "user", text="hello")
    assistant, _ = writer.create_message(task_id, "assistant", status="running", text="")
    writer.append_text(task_id, assistant.id, "world")
    writer.finish_message(task_id, assistant.id, "complete")

    state = ConversationStateService().build_initial_history_state(task_id)

    assert [message["role"] for message in state["messages"]] == ["user", "assistant"]
    assert state["messages"][0]["parts"][0]["text"] == "hello"
    assert state["messages"][1]["parts"][0]["text"] == "world"
    assert state["revision"] == 4
    assert user.id > 0


def test_run_snapshot_requires_canonical_run_messages() -> None:
    """A run snapshot cannot be fabricated from the legacy turns table."""
    task_id = _task_id()
    turn_id = TurnCrud().create(task_id, "hello").id
    writer = ConversationMutationWriter()
    assistant, _ = writer.create_message(
        task_id, "assistant", turn_id=turn_id, status="running", text=""
    )
    state = ConversationStateService().build_initial_state(task_id, turn_id, "ignored")

    assert assistant.id > 0
    assert state["run"] == {"runId": turn_id, "status": "pending"}
    assert state["messages"][0]["role"] == "assistant"


def test_cancel_run_commits_turn_and_assistant_fact_together() -> None:
    """取消必须同时落定运行与助手消息，并推进同一个 revision。"""
    task_id = _task_id()
    turn_id = TurnCrud().create(task_id, "hello").id
    writer = ConversationMutationWriter()
    assistant, _ = writer.create_message(
        task_id, "assistant", turn_id=turn_id, status="running", text=""
    )

    result = writer.cancel_run(turn_id)
    assert result is not None
    state = ConversationStateService().build_initial_history_state(task_id)
    assert state["messages"][0]["status"] == "cancelled"
    assert state["messages"][0]["endReason"] == "user_cancelled"
    assert result.revision == state["revision"]
    assert assistant.id > 0


def test_tool_call_is_projected_at_part_sequence_and_keeps_structured_result() -> None:
    """Tool UI state is attached to a stable assistant part, not appended by projection order."""
    task_id = _task_id()
    turn_id = TurnCrud().create(task_id, "inspect").id
    writer = ConversationMutationWriter()
    assistant, _ = writer.create_message(
        task_id, "assistant", turn_id=turn_id, status="running", text="thinking"
    )
    call, _ = writer.create_tool_call(
        task_id, "call-1", "read_file", {"path": "README.md"}, turn_id=turn_id
    )
    writer.transition_tool_call(task_id, "call-1", "running", turn_id=turn_id)
    writer.complete_tool_call_by_external_id(
        task_id,
        "call-1",
        {"content": "ok", "output_truncated": False},
        turn_id=turn_id,
    )

    state = ConversationStateService().build_initial_history_state(task_id)
    message = next(item for item in state["messages"] if item["id"] == f"message-{assistant.id}")
    assert [part["type"] for part in message["parts"]] == ["text", "tool-call"]
    tool_part = message["parts"][1]
    assert tool_part["toolCallId"] == "call-1"
    assert tool_part["status"] == "completed"
    assert tool_part["result"]["content"] == "ok"
    assert call.part_id is not None


def test_terminal_tool_call_cannot_be_overwritten_by_late_completion() -> None:
    """A late executor callback must not turn a cancelled call into success."""
    task_id = _task_id()
    turn_id = TurnCrud().create(task_id, "cancel").id
    writer = ConversationMutationWriter()
    writer.create_message(task_id, "assistant", turn_id=turn_id, status="running", text="")
    writer.create_tool_call(task_id, "call-2", "execute_terminal", {}, turn_id=turn_id)
    writer.transition_tool_call(task_id, "call-2", "running", turn_id=turn_id)
    writer.complete_tool_call_by_external_id(
        task_id, "call-2", None, status="cancelled", turn_id=turn_id
    )
    writer.complete_tool_call_by_external_id(
        task_id, "call-2", {"exit_code": 0}, status="completed", turn_id=turn_id
    )

    call = next(
        item for item in ConversationStateService()._tool_calls.list_by_task(task_id)
        if item.tool_call_id == "call-2"
    )
    assert call.status == "cancelled"
