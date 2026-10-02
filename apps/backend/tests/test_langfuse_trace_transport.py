from __future__ import annotations

from app.assistant_transport.event.run_event import RunTraceUpdatedEvent
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot


def _snapshot() -> ConversationStateSnapshot:
    return {
        "runs": [
            {
                "runId": 7,
                "status": "running",
                "endReason": None,
                "langfuseTraceId": None,
                "messages": [
                    {"id": "user-7", "role": "user", "parts": []},
                    {"id": "assistant-7", "role": "assistant", "parts": []},
                ],
                "usage": None,
                "error": None,
            }
        ],
        "current_run_id": 7,
        "approvals": {},
        "context_window_total": None,
        "error": None,
    }


def test_run_trace_event_projects_langfuse_trace_id() -> None:
    event = RunTraceUpdatedEvent(task_id=1, run_id=7, trace_id="trace-7")

    mutations = event.plan(_snapshot())

    assert [(mutation.path, mutation.value) for mutation in mutations] == [
        (("runs", 0, "langfuseTraceId"), "trace-7"),
    ]
