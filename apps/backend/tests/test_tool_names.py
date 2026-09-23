from app.core.tools.schemas.tool_names import ALL_TOOL_NAMES, TOOL_DELEGATE_TASK


def test_builtin_tool_names_are_unique_and_delegate_name_is_canonical() -> None:
    assert len(ALL_TOOL_NAMES) == len(set(ALL_TOOL_NAMES))
    assert TOOL_DELEGATE_TASK == "delegate_task"
