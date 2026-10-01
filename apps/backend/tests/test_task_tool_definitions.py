"""Task 工具 schema 固化与运行期允许集合的契约测试。"""

from typing import Any

from pydantic import BaseModel

from app.core.tools.schemas.tool_definition import ToolDefinition
from app.core.tools.task_tool_definitions import (
    freeze_tool_definitions,
    tool_names_from_schemas,
)
from app.storage.model.task_model import TaskModel


class _Args(BaseModel):
    value: str


def _tool(name: str, description: str) -> ToolDefinition:
    """构造测试用工具定义。"""

    return ToolDefinition(
        name=name,
        group="测试",
        description=description,
        permission=name,
        handler=lambda **_: None,
        args_model=_Args,
    )


def test_freeze_tool_definitions_keeps_model_schema_order_and_is_detached() -> None:
    """Task 固化的 schema 保持顺序，且不与进程工具定义共享可变对象。"""

    definitions = [_tool("read_file", "读取文件"), _tool("write_file", "写入文件")]

    frozen = freeze_tool_definitions(definitions)

    assert [item["name"] for item in frozen] == ["read_file", "write_file"]
    assert frozen[0]["description"] == "读取文件"
    assert frozen[0]["parameters"] == definitions[0].to_model_tool_definition()["parameters"]
    assert frozen[0] is not definitions[0].to_model_tool_definition()

    definitions[0].parameters_schema["properties"] = {"changed": {"type": "string"}}  # type: ignore[index]
    assert frozen[0]["parameters"]["properties"] == {"value": {"title": "Value", "type": "string"}}


def test_tool_names_from_schemas_preserves_schema_order_without_duplicates() -> None:
    """从 Task schema 读取工具名时保持首次出现顺序。"""

    schemas: list[dict[str, Any]] = [
        {"name": "read_file", "description": "", "parameters": {}},
        {"name": "write_file", "description": "", "parameters": {}},
        {"name": "read_file", "description": "", "parameters": {}},
    ]

    assert tool_names_from_schemas(schemas) == ("read_file", "write_file")


def test_task_model_declares_persisted_tool_definitions_without_version_fields() -> None:
    """Task 直接持有固定工具 schema，且不引入版本字段。"""

    columns = set(TaskModel.__table__.columns.keys())

    assert "tool_definitions" in columns
    assert not any("version" in column_name for column_name in columns)
