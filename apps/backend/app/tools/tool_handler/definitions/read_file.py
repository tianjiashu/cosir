"""read_file 工具定义与 handler。"""

import functools
from pathlib import Path
from typing import Optional

from app.tools.registry.helpers import tool_error, tool_result
from app.tools.schemas.tool_definition import ToolDefinition
from app.tools.tool_handler.schemas.read_file_schema import READ_FILE_PARAMETERS


def read_file_handler(
    project_root: str,
    path: str,
    limit: Optional[int] = None,
) -> str:
    """读取项目内文件内容，受项目根目录沙箱约束。

    参数:
        project_root: 项目根目录，由注册时通过 functools.partial 绑定。
        path: 相对于项目根目录的文件路径。
        limit: 可选，最多返回的行数。

    返回:
        文件内容的字符串（成功）或 JSON 错误字符串（失败）。

    异常:
        无。路径越界与读取错误均以 tool_error 返回，不抛出。

    副作用:
        读取磁盘文件（只读）。
    """
    root = Path(project_root).resolve()
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        return tool_error(f"path escapes project root: {path}")
    if not target.is_file():
        return tool_error(f"file not found: {path}")
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return tool_error(f"read failed: {exc}")
    if limit is not None and limit > 0:
        text = "\n".join(text.splitlines()[:limit])
    return text


def build_read_file_definition(project_root: str) -> ToolDefinition:
    """构造绑定到 project_root 的 read_file 工具定义。

    参数:
        project_root: 项目根目录，闭包注入到 handler。

    返回:
        ToolDefinition 实例。

    异常:
        无。

    副作用:
        无。

    注意:
        handler 使用 functools.partial 绑定 project_root，确保可被 pickle
        传入子进程执行（execute.py 的进程隔离要求 handler 可序列化）。
    """
    return ToolDefinition(
        name="read_file",
        description="读取项目内文件内容，受项目根目录沙箱约束。",
        permission="safe_read",
        required_params=tuple(READ_FILE_PARAMETERS.get("required", [])),
        handler=functools.partial(read_file_handler, project_root),
        parameters_schema=READ_FILE_PARAMETERS,
        timeout_seconds=10.0,
        risk_level="low",
    )
