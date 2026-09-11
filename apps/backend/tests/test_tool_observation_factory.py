"""ToolObservation 工厂的 Agent/UI/内部数据隔离测试。"""

from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success


def test_success_keeps_ui_and_internal_data_separate() -> None:
    """成功观察只把显式 display_data 写入 UI 通道，内部事实保持独立。"""

    data = {"kind": "file-changes", "files": [{"path": "a.py"}]}
    internal_data = {"changes": [{"path": "a.py", "before": "old"}]}
    observation = tool_success(
        "write_file",
        "file_write",
        "updated a.py",
        display_data=data,
        artifact_data=internal_data,
    )

    assert observation.display_data == data
    assert observation.artifact_data == internal_data
    assert observation.display_data is not observation.artifact_data
    assert set(observation.display_data) == {"kind", "files"}
    assert "content" not in observation.display_data
    assert "status" not in observation.display_data


def test_error_does_not_merge_observation_fields_into_ui_data() -> None:
    """失败观察的 UI display_data 不应被 error/content/reason 等 Agent 字段污染。"""

    observation = tool_error(
        "execute_terminal",
        "command failed",
        "fix the command",
        status_hint="命令失败",
    )

    assert observation.display_data == {"status_hint": "命令失败"}
    assert observation.artifact_data == {}
    assert set(observation.display_data) == {"status_hint"}
    assert "error" not in observation.display_data
    assert "reason" not in observation.display_data


def test_error_uses_short_tool_default_and_rejects_long_ui_text() -> None:
    """错误省略提示时按工具默认值，过长提示不应把诊断泄漏到 UI。"""

    defaulted = tool_error("read_file", "missing", "fix it")
    assert defaulted.display_data == {"status_hint": "读取失败"}

    replace_defaulted = tool_error("replace", "missing", "fix it")
    assert replace_defaulted.display_data == {"status_hint": "替换失败"}

    long_hint = tool_error("read_file", "missing", "fix it", status_hint="完整错误原因泄漏到前端")
    assert long_hint.display_data == {"status_hint": "读取失败"}
