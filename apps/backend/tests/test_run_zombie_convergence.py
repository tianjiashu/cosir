"""僵尸 running 防治：快照分叉时的丢弃必须留痕。

执行释放与 Run 失败边界的不变量见 ``test_workflow_session_execution`` 与
``test_run_failure_settlement``。
"""

from __future__ import annotations

import logging

from app.assistant_transport.projection.event_projection import plan_event
from app.config.logging.logger import log
from app.core.events import RunInitializedEvent

_TASK_ID = 7

_SNAPSHOT = {
    "runs": [],
    "current_run_id": None,
    "approvals": {},
    "context_window_total": None,
    "error": None,
}


def _capture_warnings() -> tuple[list[logging.LogRecord], logging.Handler]:
    # 注意：不能用「实例属性覆盖 emit」的写法——CPython 的 logging C 加速实现会绕过
    # 实例字典直接调用类型上的 emit，只有真正的 Handler 子类才会被调用。
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    log.addHandler(handler)
    return records, handler


def test_run_initialized_skeleton_drop_is_logged() -> None:
    """快照分叉导致骨架不建立时必须留 WARNING 日志，不再静默丢弃。"""

    diverged = {
        **_SNAPSHOT,
        "current_run_id": 12,
        "runs": [
            {
                "runId": 12,
                "status": "running",
                "endReason": None,
                "messages": [],
                "usage": None,
                "error": None,
            }
        ],
    }
    records, handler = _capture_warnings()
    try:
        assert plan_event(RunInitializedEvent(task_id=_TASK_ID, run_id=11), diverged) == []
    finally:
        log.removeHandler(handler)
    assert any(
        record.getMessage() == "transport_run_initialized_skeleton_dropped"
        and record.levelno == logging.WARNING
        for record in records
    )


def test_run_status_transition_drop_is_logged() -> None:
    """终态迁移被白名单丢弃时同样必须留痕（completed 不可逆分支）。"""

    from app.core.events import RunStatusChangedEvent
    from app.models.enums.conversation_run_status import ConversationRunStatus

    settled = {
        **_SNAPSHOT,
        "current_run_id": 11,
        "runs": [
            {
                "runId": 11,
                "status": "completed",
                "endReason": None,
                "messages": [],
                "usage": None,
                "error": None,
            }
        ],
    }
    records, handler = _capture_warnings()
    try:
        assert (
            plan_event(
                RunStatusChangedEvent(
                    task_id=_TASK_ID, run_id=11, status=ConversationRunStatus.RUNNING
                ),
                settled,
            )
            == []
        )
    finally:
        log.removeHandler(handler)
    assert any(
        record.getMessage() == "transport_run_status_transition_dropped" for record in records
    )
