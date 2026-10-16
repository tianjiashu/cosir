"""结构化输出后改写 Run ``final_output`` 的终态字段契约。

锁定「终态字段改写 ≠ 状态迁移」这条边界：只替换 ``final_output`` 与用量列，不发布状态事件，
也不会把已取消 / 仍在运行的 Run「修回」成成功结果。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from app.core.workflows.conversation_run_usage_stats import ConversationRunUsageStats
from app.models import ConversationRunStatus
from app.service.conversation_run import conversation_run_state_service as state_module
from app.service.depends import (
    get_conversation_run_crud,
    get_conversation_run_state_service,
    get_task_crud,
    get_workspace_crud,
)

pytestmark = pytest.mark.usefixtures("isolated_storage")


def _new_run(status: str) -> int:
    """建一个归属新 task 的 Run，返回 run_id（每个用例独立 workspace，避免跨用例污染）。"""

    workspace = get_workspace_crud().create("structured-output", str(Path.cwd()))
    task = get_task_crud().create(workspace.id, "task")
    return get_conversation_run_crud().create(task.id, "你好", status=status).id


def _completed_run() -> int:
    """返回一个已按普通最终回复自然收束（``completed``）的 Run 标识。"""

    run_id = _new_run(ConversationRunStatus.RUNNING.value)
    assert (
        get_conversation_run_state_service().complete_run_if_running(
            run_id, final_output="普通最终回复"
        )
        is not None
    )
    return run_id


def test_fix_final_output_replaces_completed_output_and_records_usage() -> None:
    run_id = _completed_run()
    usage = ConversationRunUsageStats()
    usage.add_usage_metadata({"input_tokens": 11, "output_tokens": 7, "total_tokens": 18})

    record = get_conversation_run_state_service().fix_final_output_if_completed(
        run_id, final_output='{"status":"done"}', usage_stats=usage
    )

    assert record is not None
    stored = get_conversation_run_crud().get(run_id)
    assert stored.final_output == '{"status":"done"}'
    # 改写不迁移状态：Run 仍是 completed，只是结果换成了结构化 JSON。
    assert stored.status == ConversationRunStatus.COMPLETED.value
    assert stored.usage is not None and stored.usage["total_tokens"] == 18


def test_fix_final_output_publishes_no_status_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """终态不重复迁移，因此这条路径必须零事件（否则等于重复发布 completed）。"""

    run_id = _completed_run()
    events: list[object] = []
    monkeypatch.setattr(state_module, "dispatch_conversation_event", events.append)

    assert (
        get_conversation_run_state_service().fix_final_output_if_completed(
            run_id, final_output='{"status":"done"}'
        )
        is not None
    )
    assert events == []


def test_fix_final_output_skips_cancelled_run_without_touching_columns() -> None:
    """已取消的 Run 不得被结构化结果改写（并发取消与结构化输出的竞争走这条路径）。"""

    run_id = _new_run(ConversationRunStatus.RUNNING.value)
    service = get_conversation_run_state_service()
    assert service.cancel_run_if_running(run_id, "user_cancelled") is not None

    assert service.fix_final_output_if_completed(run_id, final_output='{"status":"done"}') is None
    stored = get_conversation_run_crud().get(run_id)
    assert stored.status == ConversationRunStatus.CANCELLED.value
    assert stored.final_output is None


def test_fix_final_output_without_any_field_is_a_noop() -> None:
    run_id = _completed_run()
    before = get_conversation_run_crud().get(run_id)

    assert get_conversation_run_state_service().fix_final_output_if_completed(run_id) is None

    after = get_conversation_run_crud().get(run_id)
    assert after.final_output == before.final_output
    assert after.usage == before.usage


def test_update_fields_if_in_rewrites_only_the_named_columns() -> None:
    """通用列改写原语：白名单命中时只改指定列，typed JSON 列按存储口径序列化。"""

    run_id = _completed_run()
    crud = get_conversation_run_crud()

    record = crud.update_fields_if_in(
        run_id,
        {"final_output": "改写后", "usage_json": {"input_tokens": 3, "total_tokens": 3}},
        (ConversationRunStatus.COMPLETED.value,),
    )

    assert record is not None
    stored = crud.get(run_id)
    assert stored.final_output == "改写后"
    assert stored.usage == {"input_tokens": 3, "total_tokens": 3}
    assert stored.status == ConversationRunStatus.COMPLETED.value
    assert stored.end_reason is None
