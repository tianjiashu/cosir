"""ToolObservation 工厂：模型正文与 UI 展示数据隔离，以及等待（占位）的推导与落库往返。"""

import pytest
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import AwaitDecision, AwaitMessages, RunInBackground, ToolObservation
from app.core.tool.schemas.tool_names import TOOL_EDIT_FILE, TOOL_READ_FILE
from app.core.workflow.execution_state.tool_batch import ToolBatch
from app.models.message_context.message_metadata.tool_message_metadata import BackgroundWork


def test_success_keeps_ui_and_internal_data_separate() -> None:
    """成功观察只把显式 display_data 写入 UI 通道，不混入观察的其它字段。"""

    data = {"kind": "file-changes", "files": [{"path": "a.py"}]}
    observation = tool_success(
        "write_file",
        "file_write",
        "updated a.py",
        display_data=data,
    )

    assert observation.display_data == data
    assert set(observation.display_data) == {"kind", "files"}
    assert "content" not in observation.display_data
    assert "status" not in observation.display_data


def test_error_does_not_merge_observation_fields_into_ui_data() -> None:
    """失败观察的 UI display_data 不应被模型可见的 content 污染。"""

    observation = tool_error(
        "execute_terminal",
        "command failed. Fix the command.",
        status_hint="命令失败",
    )

    assert observation.display_data == {"status_hint": "命令失败"}
    assert set(observation.display_data) == {"status_hint"}
    assert "content" not in observation.display_data


def test_error_uses_short_tool_default_and_rejects_long_ui_text() -> None:
    """错误省略提示时按工具默认值，过长提示不应把诊断泄漏到 UI。"""

    defaulted = tool_error(TOOL_READ_FILE, "missing. Fix it.")
    assert defaulted.display_data == {"status_hint": "读取失败"}

    # edit_file 的失败短提示。
    replace_defaulted = tool_error(TOOL_EDIT_FILE, "missing. Fix it.")
    assert replace_defaulted.display_data == {"status_hint": "编辑失败"}

    long_hint = tool_error(TOOL_READ_FILE, "missing. Fix it.", status_hint="完整错误原因泄漏到前端")
    assert long_hint.display_data == {"status_hint": "读取失败"}


# 目的：三种等待互斥，挂起与否由它推导；批次随执行状态落库，读回必须还原成同一种。潜在缺陷：判别键丢了，
# 「等决定」读回变成「等消息」，决定入口找不到它；或后台占位被当成要挂起。
@pytest.mark.parametrize(
    ("waiting", "suspend", "awaiting_decision", "has_background"),
    [
        (None, False, False, False),
        (AwaitDecision(), True, True, False),
        (AwaitMessages(), True, False, False),
        (
            RunInBackground(BackgroundWork(run_id=1, label="build", started_at="t0")),
            False,
            False,
            True,
        ),
    ],
)
def test_waiting_round_trips_through_the_persisted_batch(
    waiting, suspend, awaiting_decision, has_background
) -> None:
    batch = ToolBatch(
        observations=[ToolObservation(tool_name="t", status="success", waiting=waiting)]
    )

    [restored] = ToolBatch.model_validate_json(batch.model_dump_json()).observations

    assert restored.waiting == waiting
    assert (restored.suspend, restored.awaiting_decision) == (suspend, awaiting_decision)
    assert (restored.background is not None) == has_background
