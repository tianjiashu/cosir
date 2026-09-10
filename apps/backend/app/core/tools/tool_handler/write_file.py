"""write_file 工具实现。

本模块只承载 write_file 这一个工具。写盘经由 ``file_io.atomic_write`` 做原子写
并保留目标文件既有 BOM/CRLF；落盘前做行号污染门禁（拒绝把 read_file 的带行号
输出回写），成功后额外返回统一 diff 展示数据。落盘后经 ``guard.syntax_check``
做多语言语法检查（error 驱动）：命中语法错误返回 error 观察（文件已写），
经 ``reason`` 引导 Agent 二次编辑覆盖自修复。

设计边界：
- 路径安全委托 ``security.ProjectPathResolver``，不内联路径规则。
- 执行逻辑只提供文件修改前后的事实元数据；diff 展示投影由 ``file_change_display`` 收口。
- 语法检查委托 ``guard.syntax_check``（多语言单一来源），不内联校验。
"""

import dataclasses
from pathlib import Path

from app.core.tools.guard.syntax_check import check_source_syntax, format_syntax_reason
from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import (
    blocked_device_reason,
    os_error_message,
    tool_error,
)
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.file_io.atomic_write import (
    atomic_write_text,
    looks_like_line_numbered,
)
from app.core.tools.display.file_change_display import (
    build_file_change_display_data,
)
from app.core.tools.tool_handler.patch.patch_diff import FileDiffResult
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.write_file_args import WriteFileArgs


class WriteFileTool(HandlerBase):
    """把文本内容原子写入项目内文件的工具类。

    参数:
        无。

    返回:
        ``WriteFileTool`` 实例。

    异常:
        初始化阶段不主动抛出业务异常。

    副作用:
        仅保存项目根与解析器；不读取、不写入文件。
    """

    name = "write_file"
    description = (
        "Write a file with the provided content, creating parent directories as needed. "
        "Uses atomic write and preserves the target file's existing CRLF/BOM. Refuses to "
        "write content that looks like line-numbered read_file output. Returns a unified "
        "diff for display."
    )
    permission = "file_write"
    args_model = WriteFileArgs
    timeout_seconds = 30.0
    risk_level = "medium"

    def __init__(self) -> None:
        """初始化 write_file 工具实例。

        参数:
            无。

        返回:
            无。

        异常:
            无。
        """

    def execute(
        self,
        path: str,
        content: str,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """把内容原子写入项目内文件，并返回结构化观察结果。

        参数:
            path: 待写入的文件路径（相对项目根）。
            content: 待写入的内容。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；
                由执行链在执行期强制注入，handler 契约必须接受此 kwarg。
                破坏性操作以其 ``workspace_root`` 作为路径 containment 的唯一事实源。

        返回:
            ``ToolObservation``；成功时 content 保持为写入内容，
            失败时 status 为 error，``error``/``reason`` 提供面向模型的富文本诊断
            （``error``=发生了什么、``reason``=为什么失败+如何修正+是否重试）。

        异常:
            不主动向上抛出；路径/设备/行号/写入错误都被转换为结构化观察。

        副作用:
            可能创建父目录并原子写入目标文件。
        """
        workspace_root = execution_context.workspace_root
        resolver = PathResolver(workspace_root)
        device_error = resolver.blocked_device_reason(path)
        if device_error:
            return tool_error(
                self.name,
                device_error,
                reason=blocked_device_reason("written"),
                permission=self.permission,
            )
        resolved, error = resolver.resolve_within_workspace(path)
        if resolved is None:
            return tool_error(
                self.name,
                f"could not write the file: {error}",
                reason=(
                    "the path escapes the project workspace and cannot be written. The "
                    "resolver rejects paths that point outside the workspace root for "
                    "safety. Pass a path inside the project (relative to the workspace "
                    "root, or an absolute path under it); the same out-of-bounds path "
                    "will always be rejected."
                ),
                permission=self.permission,
            )
        device_error = resolver.blocked_device_reason(path, resolved)
        if device_error:
            return tool_error(
                self.name,
                device_error,
                reason=blocked_device_reason("written"),
                permission=self.permission,
            )
        if looks_like_line_numbered(content):
            return tool_error(
                self.name,
                "content looks like line-numbered read_file output: most lines start "
                "with a 'N| ' prefix (e.g. '12| code'). These prefixes are display "
                "metadata, not file content. Strip the leading 'N| ' from every line "
                "and retry with the raw content only.",
                reason=(
                    "the content you are trying to write looks like line-numbered "
                    "read_file output (most lines start with a 'N| ' prefix). These "
                    "prefixes are display metadata added by read_file, not real file "
                    "content. Strip the leading 'N| ' from every line and retry with the "
                    "raw content only; the same content will always be rejected."
                ),
                permission=self.permission,
            )
        existed = resolved.exists()
        original = ""
        if existed:
            try:
                original = Path(resolved).read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return tool_error(
                    self.name,
                    os_error_message(exc, "read the file"),
                    reason=(
                        "the existing file could not be read before writing, usually "
                        "because it is locked by another process or the current user lacks "
                        "read permission. Close the program holding the file or adjust "
                        "permissions, then retry the same write."
                    ),
                    retryable=True,
                    permission=self.permission,
                )

        try:
            atomic_write_text(
                resolved,
                content,
                containment_root=workspace_root,
            )
        except OSError as exc:
            return tool_error(
                tool_name=self.name,
                error=os_error_message(exc, "write the file"),
                reason=(
                    "the write failed, usually because the target file is locked by "
                    "another process or the current user lacks write permission in that "
                    "directory; close the program holding the file or adjust permissions, "
                    "then retry the same write."
                ),
                retryable=True,
                permission=self.permission,
            )

        # 落盘后语法检查（error 驱动）：命中语法错误返回 error 观察（文件已写），
        # 经 reason 引导 Agent 二次编辑覆盖自修复；data 只给前端展示。
        result = check_source_syntax(str(resolved), content)
        if result.has_error:
            return tool_error(
                tool_name=self.name,
                error="syntax error detected after write",
                reason=format_syntax_reason(result),
                permission=self.permission,
                display_data={"syntax_errors": [dataclasses.asdict(d) for d in result.diagnostics]},
            )

        status = "modified" if existed else "added"
        snapshot = FileDiffResult(path=path, status=status, before=original, after=content)
        display_data = build_file_change_display_data([snapshot])
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=content,
            display_data={"kind": "file-changes", **display_data},
            artifact_data=display_data,
        )

    def to_definition(self) -> ToolDefinition:
        """把工具实例转换成 ``ToolDefinition``。

        参数:
            无。

        返回:
            可直接注册到 ``ToolRegistry`` 的工具定义（含 display 展示元数据）。

        异常:
            无。

        副作用:
            无。
        """

        return ToolDefinition(
            name=self.name,
            description=self.description,
            permission=self.permission,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            resource_keys=("filesystem",),
            display=ToolDisplayHints(
                verb="写入文件",
                icon="git-compare",
                surface="standalone",
                expandable=True,
                expand_layout="diff",
            ),
        )


def build_write_file_definition() -> ToolDefinition:
    """构造 write_file 工具定义。

    参数:
        无。

    返回:
        ``ToolDefinition``，供 ``ToolRegistry`` 注册。

    异常:
        无。

    副作用:
        创建 ``WriteFileTool`` 实例和定义对象，不执行文件写入。
    """

    return WriteFileTool().to_definition()
