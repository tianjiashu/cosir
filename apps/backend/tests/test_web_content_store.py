from pathlib import Path

from app.tools.schemas.tool_execution_context import ToolExecutionContext
from app.tools.tool_handler.web.web_content_store import (
    convert_base64_images_to_placeholders,
    truncate_or_store_content,
)


def test_replaces_inline_base64_images_with_placeholder() -> None:
    """验证内联 base64 图片被替换为简洁占位符。

    参数:
        无。

    返回:
        无。

    异常:
        无。

    副作用:
        无。
    """

    content = "Before ![diagram](data:image/png;base64,AAAABBBB) after"

    assert convert_base64_images_to_placeholders(content) == "Before [IMAGE: diagram] after"


def test_stores_full_content_and_returns_read_file_footer(tmp_path: Path) -> None:
    """验证超长网页内容存入工作区并返回分页读取提示。

    参数:
        tmp_path: pytest 提供的隔离工作区根目录。

    返回:
        无。

    异常:
        无。

    副作用:
        在临时工作区创建网页内容存储文件。
    """

    context = ToolExecutionContext(
        task_id="task_1",
        workspace_id="workspace_1",
        workspace_root=tmp_path,
    )
    content = "A" * 80 + "MIDDLE" + "Z" * 80

    truncated, stored = truncate_or_store_content(
        content=content,
        url="https://example.com/article",
        title="Article",
        execution_context=context,
        char_limit=80,
    )

    assert stored is not None
    assert stored.path.is_file()
    assert stored.relative_path.startswith(".coding-agent/tool-results/web/")
    assert "Content truncated" in truncated
    assert "read_file" in truncated
    assert "MIDDLE" not in truncated
    assert stored.path.read_text(encoding="utf-8") == content
