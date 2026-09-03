"""read_file 工具实现。

本模块只承载 read_file 这一个工具。为了符合“一个工具一个类”的约定，
路径安全、文本读取、分页、二进制判断、行号渲染和工具定义适配都封装在
``ReadFileTool`` 内部。

设计边界：
- 参数校验模型放在 ``app.tools.tool_models.ReadFileArgs``，由 Pydantic 负责。
- 对外注册仍通过 ``build_read_file_definition`` 返回 ``ToolDefinition``，暂不改变注册逻辑。
- 工具执行只读文件系统，不写入任何文件，不执行 shell 命令。
"""

import dataclasses
import json
from pathlib import Path
from typing import ClassVar

from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import os_error_message, tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models import ReadFileArgs
from app.core.tools.tool_models.text_read_result import TextReadResult

# 「为什么失败」富文本：路径指向系统设备/敏感伪文件，无法读取（read_file 两处
# blocked_device 分支共用，避免重复长串）。与新契约一致：reason 不再是短码。
_BLOCKED_DEVICE_REASON = (
    "the requested path points to an OS device or sensitive pseudo-file "
    "(e.g. NUL/CON/COM1 on Windows, /dev/* or /proc/* on POSIX) and cannot be read; "
    "pass a regular text file path inside the project instead. The same path will "
    "always be rejected, so choose a different file."
)


class ReadFileTool(HandlerBase):
    """读取项目内文本文件的工具类。

    这个类是 read_file 的唯一实现载体。它把工具元信息、执行入口、路径安全、
    文本分页和输出格式化放在同一个类中，便于后续按“一个工具一个类”的方式
    继续扩展其它工具。

    参数:
        project_root: 相对路径的解析基准目录（workspace 根）。作为只读工具，
            read_file 不强制 containment：相对路径以该目录为基准解析，绝对/越界
            路径也允许读取（仅设备/伪文件路径被拦截）。

    返回:
        ``ReadFileTool`` 实例。注册阶段会通过 ``to_definition`` 转成
        ``ToolDefinition``。

    异常:
        初始化阶段不主动抛出业务异常。执行阶段会把路径错误、文件错误和读取错误
        转换成结构化 ``ToolObservation``。

    副作用:
        仅保存项目根目录路径；不读取、不写入文件。
    """

    name = "read_file"
    description = (
        "Read a text file with line numbers and pagination. Use this instead of "
        "cat/head/tail in terminal. Output format: 'LINE_NUM| CONTENT'. Use offset "
        "and limit for large files. Reads exceeding about 100K characters are "
        "truncated on a line boundary and return a next_offset; continue with "
        "offset to read the rest. NOTE: Cannot read images or other binary files."
    )
    permission = "safe_read"
    args_model = ReadFileArgs
    timeout_seconds = 10.0
    risk_level = "low"

    binary_extensions: ClassVar[set[str]] = {
        ".7z",
        ".avi",
        ".bin",
        ".bmp",
        ".class",
        ".dll",
        ".doc",
        ".docx",
        ".exe",
        ".gif",
        ".ico",
        ".jar",
        ".jpg",
        ".jpeg",
        ".mov",
        ".mp3",
        ".mp4",
        ".pdf",
        ".png",
        ".ppt",
        ".pptx",
        ".pyc",
        ".rar",
        ".so",
        ".tar",
        ".webp",
        ".xls",
        ".xlsx",
        ".zip",
    }

    max_limit = 2_000
    max_content_chars = 100_000
    max_line_chars = 2_000
    binary_sample_bytes = 4_096
    utf8_bom = "\ufeff"

    def __init__(self) -> None:
        """初始化 read_file 工具实例。

        参数:
            无
        返回:
            无。

        异常:
            无。
        """

    def execute(
        self,
        path: str,
        execution_context: ToolExecutionContext,
        offset: int = 1,
        limit: int = 500,
    ) -> ToolObservation:
        """读取项目目录内的文本文件，并返回适合模型消费的观测结果。

        参数:
            path: 用户或模型请求读取的文件路径。相对路径以 workspace 根为基准解析，
                也接受项目根外的绝对路径（只读不受 workspace 边界限制）。
            offset: 从第几行开始读取，1 表示第一行。
            limit: 最多返回多少行。参数模型会限制最大值，内部也会再次归一化。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；由执行链
                在子进程内无条件注入的关键字参数，handler 契约必须接受此 kwarg 以匹配
                ``ToolExecutor._execute_handler`` 调用约定；本工具只读且不受 workspace
                边界限制，故不消费该值。

        返回:
            ``ToolObservation``。成功时 ``content`` 包含 ``LINE_NUM|CONTENT`` 格式的
            带行号文本；失败时 ``status`` 为 ``error``，``error``/``reason`` 提供面向模型的
            富文本诊断（``error``=发生了什么、``reason``=为什么失败+如何修正+是否重试）。

        异常:
            不主动向上抛出异常。文件不存在、路径无法解析、二进制文件和读取失败都会被
            转换成结构化 ``ToolObservation``。

        副作用:
            只读文件系统，不写入任何文件。
        """
        root = execution_context.workspace_root
        resolver = PathResolver(root)
        ## 阶段1：对原始字符串做设备名/posix 禁止路径的 fail-fast 拦截
        device_error = resolver.blocked_device_reason(path)
        if device_error:
            return tool_error(
                self.name,
                device_error,
                reason=_BLOCKED_DEVICE_REASON,
                permission=self.permission,
            )

        # 解析路径、相对路径转绝对路径
        resolved, error = resolver.resolve_without_boundary(path)
        if resolved is None:
            return tool_error(
                self.name,
                f"could not read the file: {error}",
                reason=(
                    "the path argument could not be resolved to a readable file "
                    "(common causes: empty value, NUL characters, or a malformed path). "
                    "Provide a valid, non-empty file path -- absolute, or relative to the "
                    "project root -- and retry; the same invalid value will always fail."
                ),
                permission=self.permission,
            )

        device_error = resolver.blocked_device_reason(path, resolved)
        if device_error:
            return tool_error(
                self.name,
                device_error,
                reason=_BLOCKED_DEVICE_REASON,
                permission=self.permission,
            )

        result = self._read_text_page(resolved, offset, limit)
        if result.error:
            return tool_error(
                self.name,
                result.error,
                reason=result.reason,
                retryable=result.retryable,
                permission=self.permission,
            )

        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=json.dumps(dataclasses.asdict(result)),
        )

    def to_definition(self) -> ToolDefinition:
        """把工具实例转换成当前注册系统使用的 ``ToolDefinition``。

        参数:
            无。

        返回:
            可直接注册到 ``ToolRegistry`` 的工具定义。

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
                verb="读取",
                icon="eye",
                expandable=False,
                expand_layout="none",
            ),
        )

    def _normalize_read_pagination(self, offset: int, limit: int) -> tuple[int, int]:
        """归一化分页参数，保证 offset/limit 落在安全范围内。

        参数:
            offset: 期望开始读取的 1 基行号。
            limit: 期望读取的最大行数。

        返回:
            ``(normalized_offset, normalized_limit)``。

        异常:
            ``ValueError``: 当传入对象无法转成整数时可能抛出；正常入口会先经过 Pydantic 校验。

        副作用:
            无。
        """

        normalized_offset = max(1, int(offset))
        normalized_limit = max(1, min(int(limit), self.max_limit))
        return normalized_offset, normalized_limit

    def _read_text_page(self, path: Path, offset: int, limit: int) -> TextReadResult:
        """按页读取文本文件，并返回带行号的内容。

        参数:
            path: 已经过安全解析的文件路径。
            offset: 从第几行开始读取，1 表示第一行。
            limit: 最多读取多少行。

        返回:
            ``TextReadResult``。成功时 ``content`` 包含带行号文本；失败时 ``error`` 非空。

        异常:
            不主动向上抛出文件系统异常。读取失败会返回富文本 ``reason``（含根因与重试提示）。

        副作用:
            只读文件系统，不写入任何文件。
        """

        if not path.exists():
            return TextReadResult(
                error=f"could not read the file: no such file at '{path}'",
                reason=(
                    "the file does not exist at the given path. Check for a typo, confirm "
                    "the file was not moved or deleted, or pass an absolute path. The same "
                    "non-existent path will always fail, so retry only after the file "
                    "exists or the path is corrected."
                ),
            )
        if path.is_dir():
            return TextReadResult(
                error=f"could not read '{path}': it is a directory, not a file",
                reason=(
                    "the path resolves to a directory; read_file reads only files. Point "
                    "to a specific file, or use a directory listing tool to inspect the "
                    "directory's contents. Retrying the same directory path will always fail."
                ),
            )
        if self._is_likely_binary(path):
            return TextReadResult(
                file_size=self._safe_file_size(path),
                error=(
                    "Binary file cannot be displayed as text. Use a dedicated binary "
                    "viewer or open it outside the agent to inspect its contents."
                ),
                reason=(
                    "the target is a binary file (detected by extension or NUL bytes) and "
                    "cannot be rendered as text. Use a dedicated binary viewer or open it "
                    "outside the agent; reading it with read_file will always fail."
                ),
            )

        offset, limit = self._normalize_read_pagination(offset, limit)
        selected: list[tuple[int, str]] = []
        next_offset = None
        hint = ""
        total_lines = 0
        current_chars = 0
        try:
            with path.open("r", encoding="utf-8", errors="strict") as file:
                for line_number, raw_line in enumerate(file, start=1):
                    total_lines = line_number
                    if line_number < offset:
                        continue
                    if len(selected) >= limit:
                        next_offset = line_number
                        hint = f"Use offset={next_offset} to continue reading."
                        break

                    line = raw_line.rstrip("\r\n")
                    if line_number == 1 and line.startswith(self.utf8_bom):
                        line = line[len(self.utf8_bom) :]

                    rendered = self._render_line(line_number, line)
                    addition = len(rendered) + (1 if selected else 0)
                    if current_chars + addition > self.max_content_chars:
                        next_offset = line_number
                        hint = (
                            "Output was truncated by character budget. "
                            f"Use offset={next_offset} to continue reading."
                        )
                        break

                    selected.append((line_number, line))
                    current_chars += addition
        except UnicodeDecodeError:
            return TextReadResult(
                error=(
                    "File is not valid UTF-8 and cannot be read as text. It may use a "
                    "different encoding (e.g. GBK on Windows) or be a binary file. "
                    "Re-save it as UTF-8, or convert it before reading."
                ),
                reason=(
                    "read_file refuses to silently replace invalid bytes, because doing so "
                    "would feed corrupted text (replacement characters) back to the model. "
                    "Convert the file to UTF-8 and retry the same read; the same non-UTF-8 "
                    "file will always be rejected."
                ),
            )
        except OSError as exc:
            return TextReadResult(
                error=os_error_message(exc, "read the file"),
                reason=(
                    "the read failed, usually because the file is locked by another process "
                    "or the current user lacks read permission; close the program holding "
                    "the file or adjust permissions, then retry the same read."
                ),
                retryable=True,
            )

        content = self._line_numbered_content(selected)
        if hint:
            content = f"{content}\n\n{hint}" if content else hint

        return TextReadResult(
            content=content,
            total_lines=total_lines,
            file_size=self._safe_file_size(path),
            next_offset=next_offset,
            hint=hint,
        )

    def _is_likely_binary(self, path: Path) -> bool:
        """判断文件是否像二进制文件。

        参数:
            path: 待检测文件路径。

        返回:
            True 表示应拒绝按文本展示；False 表示可以尝试按 UTF-8 文本读取。

        异常:
            不向上抛出。采样失败时返回 False，让真正读取阶段给出更准确错误。

        副作用:
            只读取文件开头少量字节。
        """

        if path.suffix.lower() in self.binary_extensions:
            return True
        try:
            with path.open("rb") as file:
                sample = file.read(self.binary_sample_bytes)
        except OSError:
            return False
        return b"\x00" in sample

    def _line_numbered_content(self, lines: list[tuple[int, str]]) -> str:
        """把多行文本渲染成带行号的工具输出。

        参数:
            lines: ``(line_number, line_text)`` 元组列表。

        返回:
            形如 ``12| print('hello')`` 的多行字符串。

        异常:
            无。

        副作用:
            无。
        """

        return "\n".join(self._render_line(line_number, line) for line_number, line in lines)

    def _render_line(self, line_number: int, line: str) -> str:
        """渲染单行文本，并在过长时截断单行内容。

        参数:
            line_number: 原始文件中的行号。
            line: 原始行文本，不包含换行符。

        返回:
            带行号前缀的单行字符串。

        异常:
            无。

        副作用:
            无。
        """

        if len(line) > self.max_line_chars:
            line = f"{line[:self.max_line_chars]}... [line truncated]"
        return f"{line_number}| {line}"

    def _safe_file_size(self, path: Path) -> int:
        """返回文件大小；读取失败时降级为 0。

        参数:
            path: 目标文件路径。

        返回:
            文件字节大小；stat 失败时返回 0。

        异常:
            不向上抛出。

        副作用:
            无。
        """

        try:
            return path.stat().st_size
        except OSError:
            return 0


def build_read_file_definition() -> ToolDefinition:
    """构造绑定到指定项目根目录的 read_file 工具定义。

    参数:
        无。

    返回:
        ``ToolDefinition``，供 ``ToolRegistry`` 注册。

    异常:
        无。

    副作用:
        创建 ``ReadFileTool`` 实例和定义对象，不执行文件读取。
    """

    return ReadFileTool().to_definition()
