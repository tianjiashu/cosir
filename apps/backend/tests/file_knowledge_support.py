"""先将文件工具真实返回的读取事实交给被测调用。"""

from pathlib import Path

from app.core.tool.schemas import ToolExecutionContext
from app.tools.tool_handler.read_file import ReadFileTool


def with_read_files(context: ToolExecutionContext, *paths: Path | str) -> ToolExecutionContext:
    for path in paths:
        result = ReadFileTool().execute(str(path), context)
        if result.facts is not None:
            context.file_knowledge.observe(result.facts)
    return context


def with_workspace_reads(context: ToolExecutionContext) -> ToolExecutionContext:
    return with_read_files(
        context, *(path for path in context.workspace_root.rglob("*") if path.is_file())
    )
