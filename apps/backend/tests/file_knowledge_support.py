"""把文件工具真实交出的事实交还给下一次调用，模拟 core 按上下文原样交还。"""

from dataclasses import replace
from pathlib import Path

from app.core.tool.schemas import ToolExecutionContext
from app.core.tool.schemas.tool_observation import ToolObservation
from app.tools.tool_handler.file.read_file import ReadFileTool


def seen(context: ToolExecutionContext, observation: ToolObservation) -> ToolExecutionContext:
    """这次结果已进入模型上下文（读取证据生效）。"""
    if observation.facts is None:
        return context
    return replace(context, visible_facts=(*context.visible_facts, observation.facts))


def in_batch(context: ToolExecutionContext, observation: ToolObservation) -> ToolExecutionContext:
    """这次结果是本批前面的调用交出的，模型还没看到（只有写入生效）。"""
    if observation.facts is None:
        return context
    return replace(context, batch_facts=(*context.batch_facts, observation.facts))


def with_read_files(context: ToolExecutionContext, *paths: Path | str) -> ToolExecutionContext:
    for path in paths:
        context = seen(context, ReadFileTool().execute(str(path), context))
    return context


def with_workspace_reads(context: ToolExecutionContext) -> ToolExecutionContext:
    return with_read_files(
        context, *(path for path in context.workspace_root.rglob("*") if path.is_file())
    )
