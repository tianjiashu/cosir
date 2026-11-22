"""child-session tool display and canonical parent delegation regressions."""

import json
from datetime import UTC, datetime

# Load the tool package before the transport event package to match the production
# dependency bootstrap and avoid the legacy import cycle during isolated collection.
import app.tools  # noqa: F401
from app.assistant_transport.projection.snapshot_rebuild import (
    pair_tool_parts,
)
from app.core.tool.multi_agent.delegate_task import DelegateTaskTool
from app.core.tool.session_management.progress import SessionProgress
from app.core.tool.session_management.session_read import SessionReadTool
from app.models.message_metadata import ToolMessageMetadata
from app.models.run.run_record import RunRecord
from app.models.session.session_context import SessionContextRecord


def _run(
    *,
    run_id: int,
    session_id: int,
    status: str,
    final_output: str | None = None,
) -> RunRecord:
    now = datetime(2026, 9, 24, tzinfo=UTC)
    return RunRecord(
        id=run_id,
        session_id=session_id,
        status=status,
        created_at=now,
        updated_at=now,
        final_output=final_output,
        end_reason=None if status == "completed" else "child_failed",
        agent_id="reviewer",
    )


def test_child_agent_display_builders_emit_stable_kinds_and_safe_fields() -> None:
    progress = SessionProgress(
        session_id=2,
        name="Review",
        kind="delegated child agent",
        status="completed",
        elapsed="took 3s",
        now="idle (run ended)",
        steps=("said \"secret step text\"",),
        queued=0,
        final_output="summary",
        end_reason=None,
    )
    result = SessionReadTool._display_data(progress)
    # 最近几步只给模型：展示数据不带过程正文。
    assert result == {
        "kind": "agent-session-result",
        "operation": "read",
        "session_id": 2,
        "agent_name": "Review",
        "final_output": "summary",
        "end_reason": None,
    }


def test_parent_delegation_display_never_carries_child_output() -> None:
    display = DelegateTaskTool._delegation_display_data(
        title="Review",
        child_agent_id="reviewer",
        child_session_id=2,
        role="Reviewer",
    )

    assert display == {
        "kind": "delegation-result",
        "title": "Review",
        "child_agent_id": "reviewer",
        "child_session_id": 2,
        "role": "Reviewer",
    }
    assert "final_output" not in display


def test_cold_rebuild_uses_persisted_delegation_display_data_without_inference(
    monkeypatch,
) -> None:
    rows = [
        SessionContextRecord(
            session_id=1,
            run_id=10,
            message={
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "delegate-call",
                        "type": "function",
                        "function": {
                            "name": "delegate_task",
                            "arguments": json.dumps(
                                {
                                    "child_agent_id": "reviewer",
                                    "agent_name": "Review",
                                    "message": "inspect",
                                }
                            ),
                        },
                    }
                ],
            },
            include_in_context=True,
            sequence=1,
            metadata={},
            turn=1,
        ),
        SessionContextRecord(
            session_id=1,
            run_id=10,
            message={"role": "tool", "content": "started", "tool_call_id": "delegate-call"},
            include_in_context=True,
            sequence=2,
            metadata=ToolMessageMetadata.model_validate(
                {
                    "status": "completed",
                    "display_data": {
                        "kind": "delegation-result",
                        "title": "Review",
                        "child_agent_id": "reviewer",
                        "child_session_id": 2,
                    },
                }
            ),
            turn=1,
        ),
    ]

    # 历史展示仅来自持久化载荷，未保存 role 时不从当前配置推断。
    parts = pair_tool_parts(rows, {})

    assert parts["delegate-call"]["display_data"] == rows[1].metadata.display_data
    assert "final_output" not in parts["delegate-call"]["display_data"]
