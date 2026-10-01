"""Task 工具 schema 工具名提取的契约测试。"""

from typing import Any

from app.core.tools.task_tool_definitions import tool_names_from_schemas
from app.storage.model.task_model import TaskModel


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
