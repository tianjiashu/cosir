"""ToolCall 值对象单元测试，锁定 from_dict 字段兜底口径。"""

from app.tools.schemas import ToolCall


def test_from_dict_full_fields():
    """完整字段经 from_dict 重建后与原值一致。"""
    data = {"tool_name": "read_file", "arguments": {"path": "a.txt"}, "call_id": "c1"}
    call = ToolCall.from_dict(data)
    assert call.tool_name == "read_file"
    assert call.arguments == {"path": "a.txt"}
    assert call.call_id == "c1"


def test_from_dict_missing_arguments_defaults_to_empty_dict():
    """缺失 arguments 时兜底为空 dict 而非 None。"""
    call = ToolCall.from_dict({"tool_name": "read_file", "call_id": "c1"})
    assert call.arguments == {}


def test_from_dict_none_arguments_defaults_to_empty_dict():
    """arguments 为 None 时兜底为空 dict（与工具执行路径一致）。"""
    call = ToolCall.from_dict(
        {"tool_name": "read_file", "arguments": None, "call_id": "c1"}
    )
    assert call.arguments == {}


def test_from_dict_missing_call_id_defaults_to_empty_string():
    """缺失 call_id 时兜底为空串。"""
    call = ToolCall.from_dict({"tool_name": "read_file", "arguments": {}})
    assert call.call_id == ""


def test_from_dict_missing_tool_name_defaults_to_empty_string():
    """缺失 tool_name 时兜底为空串，避免下标访问抛 KeyError。"""
    call = ToolCall.from_dict({"arguments": {}, "call_id": "c1"})
    assert call.tool_name == ""
