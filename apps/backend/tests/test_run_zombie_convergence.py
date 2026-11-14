"""僵尸 running 防治：快照分叉时的丢弃必须留痕。

执行释放与 Run 失败边界的不变量见 ``test_session_actors_execution`` 与
``test_run_failure_settlement``。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.assistant_transport.projection.event_projection import plan_event
from app.config.logging.logger import log

# RunInitializedEvent 的 created_at 由领域侧从 Run 记录填入；测试给固定值。
_RUN_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)

_SESSION_ID = 7

_SNAPSHOT = {"messages": [], "activity": None, "error": None}


def _activity(run_id: int, status: str) -> dict:
    return {"modelActivity": None,
        "turn": None,
        "resumable": False,
        "status": status,
        "endReason": None,
        "error": None,
        "usage": None,
        "contextWindowTotal": None,
        "startedAt": "2026-01-01T00:00:00+00:00",
        "launch": None,
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


def test_run_status_transition_drop_is_logged() -> None:
    """终态迁移被白名单丢弃时同样必须留痕（completed 不可逆分支）。"""

    from app.core.events import RunStatusChangedEvent
    from app.models.enums.run_status import RunStatus

    settled = {
        **_SNAPSHOT,
        "activity": _activity(11, "completed"),
    }
    records, handler = _capture_warnings()
    try:
        assert (
            plan_event(
                RunStatusChangedEvent(
                    changed_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
                    session_id=_SESSION_ID, run_id=11, status=RunStatus.RUNNING
                ),
                settled,
                {},
            )
            == []
        )
    finally:
        log.removeHandler(handler)
    assert any(
        record.getMessage() == "transport_run_status_transition_dropped" for record in records
    )
