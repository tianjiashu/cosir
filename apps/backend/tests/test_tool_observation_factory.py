"""ToolObservation 工厂的 Agent/UI/内部数据隔离测试。"""

from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success


def test_success_keeps_ui_and_internal_data_separate() -> None:
    """成功观察只把显式 data 写入 UI 通道，内部事实保持独立。"""

    data = {"kind": "file-changes", "files": [{"path": "a.py"}]}
    internal_data = {"changes": [{"path": "a.py", "before": "old"}]}
    observation = tool_success(
        "write_file",
        "file_write",
        "updated a.py",
        data=data,
        internal_data=internal_data,
    )

    assert observation.data == data
    assert observation.internal_data == internal_data
    assert observation.data is not observation.internal_data
    assert set(observation.data) == {"kind", "files"}
    assert "content" not in observation.data
    assert "status" not in observation.data


def test_error_does_not_merge_observation_fields_into_ui_data() -> None:
    """失败观察的 UI data 不应被 error/content/reason 等 Agent 字段污染。"""

    observation = tool_error(
        "execute_terminal",
        "command failed",
        "fix the command",
        data={"kind": "terminal-result", "exit_code": 1},
        internal_data={"diagnostic": "internal"},
    )

    assert observation.data == {"kind": "terminal-result", "exit_code": 1}
    assert observation.internal_data == {
        "diagnostic": "internal",
        "error_kind": "runtime_failed",
    }
    assert set(observation.data) == {"kind", "exit_code"}
    assert "error" not in observation.data
    assert "reason" not in observation.data
