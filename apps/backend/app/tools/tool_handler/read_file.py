"""read_file 工具实现。

本模块只承载 read_file 这一个工具。为了符合“一个工具一个类”的约定，
路径安全、文本读取、分页、二进制判断、行号渲染和工具定义适配都封装在
``ReadFileTool`` 内部。

设计边界：
- 参数校验模型放在 ``app.tools.tool_models.ReadFileArgs``，由 Pydantic 负责。
- 对外注册仍通过 ``build_read_file_definition`` 返回 ``ToolDefinition``，暂不改变注册逻辑。
- 工具执行只读文件系统，不写入任何文件，不执行 shell 命令。
- 分页由工具自己按字符预算完成，预算低于模型通道上限 ``Constant.Tools.MAX_OUTPUT_CHARS``：
  通用的 ``ToolOutputBudget`` 超限时保留头尾、挖掉中段，对文件内容来说会让模型丢失中间几十行
  且不知道丢了什么；工具内分页则在整行边界停下并给出下一段的 offset。每页都带
  ``lines a-b of N`` 页头与 ``[end of file]`` / 续读 offset 页尾，模型不必再去 ``wc -l``。
"""

from pathlib import Path
from typing import ClassVar

from app.config.constant import Constant
from app.core.tool.results.tool_error import os_error_message, tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_READ_FILE,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_SEARCH
from app.tools.display.filesystem_display import (
    build_read_file_display_data,
)
from app.tools.tool_handler.security.path_resolver import PathResolver
from app.tools.tool_models import ReadFileArgs
from app.tools.tool_models.text_read_result import TextReadResult


class ReadFileTool(HandlerBase):
    """读取文本文件并构造模型工具定义的处理器。

    开发者说明：相对路径基于执行上下文的 ``workspace_root``；只读读取允许访问
    workspace 外的绝对路径。路径解析与文本读取的失败会转换为 ``ToolObservation``。
    模型可见的用途和限制单独写在 ``description``，参数用法写在参数 schema。
    """

    name = TOOL_READ_FILE
    args_model = ReadFileArgs
    timeout_seconds = 10.0
    group = TOOL_GROUP_SEARCH

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
    # 页头与页尾占用的字符预留；正文行预算 = 模型通道上限 - 预留，保证整条结果不会再被
    # ``ToolOutputBudget`` 中段截断。
    page_frame_reserve = 200
    max_content_chars = Constant.Tools.MAX_OUTPUT_CHARS - page_frame_reserve
    max_line_chars = 2_000
    binary_sample_bytes = 4_096
    utf8_bom = "\ufeff"
    # 单文件体积硬上限：超过此值的文件一律禁止读取，分页也不放行。
    max_file_bytes = 5_000_000

    description = (
        "Read a text file as numbered lines ('LINE_NUM| CONTENT'). One call returns at most "
        f"`limit` lines and about {max_content_chars // 1000}K characters, whichever comes "
        "first. The first line reports `lines a-b of N` (N is the file's total line count) "
        "and the last line is `[end of file]` or the offset to continue from, so there is no "
        "need to count lines or page by other means. Files larger than 5 MB, images, and other "
        "binary files cannot be read."
    )

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
            execution_context: 本次执行的运行时边界；只用其中的 workspace 根解析相对路径
                （读取不受 workspace 边界限制）。

        返回:
            ``ToolObservation``。成功时 ``content`` 仅包含模型后续工作所需的
            ``LINE_NUM|CONTENT`` 文本；失败时 ``status`` 为 ``error``，``content``
            给出错误事实与下一步动作。

        异常:
            不主动向上抛出异常。文件不存在、路径无法解析、二进制文件和读取失败都会被
            转换成结构化 ``ToolObservation``。

        副作用:
            只读文件系统，不写入任何文件。
        """
        resolved, error = PathResolver(execution_context.workspace_root).resolve_for_read(path)
        if resolved is None:
            return tool_error(
                self.name, f"could not read the file: {error}.", status_hint="路径无效"
            )

        result = self._read_text_page(resolved, offset, limit)
        if result.error:
            return tool_error(
                self.name,
                result.error,
                status_hint="无法读取",
            )

        normalized_offset, _ = self._normalize_read_pagination(offset, limit)
        # 展示契约（tool_ui_display_contract.md 3.1）：读到文件尾渲染为 ``L<start>-END``，
        # 空文件与 offset 越界也算读到尾。
        end_line: int | str | None = "END" if result.next_offset is None else result.end_line
        return tool_success(
            tool_name=self.name,
            content=result.content,
            display_data=build_read_file_display_data(
                path,
                start_line=normalized_offset,
                end_line=end_line,
                file_size=result.file_size,
                truncated=result.truncated,
            ),
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
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            resource_keys=("filesystem",),
            # 只读、彼此独立：模型一次发出的多个读取真正并发执行（见 tool_batch_runner）。
            parallel_mode="parallel",
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
            ``TextReadResult``。成功时 ``content`` 为带页头页尾的行号文本（格式见
            ``_page_content``）；失败（文件不存在、是目录、超过 5MB 体积上限、二进制或编码
            错误）时 ``error`` 非空。本页在 ``limit`` 行或字符预算（``max_content_chars``）
            先到者处停下，只有后者记为 ``truncated``。

        异常:
            不主动向上抛出文件系统异常。读取失败时 ``error`` 同时给出错误事实与修正建议。

        副作用:
            只读文件系统，不写入任何文件。
        """

        if not path.exists():
            return TextReadResult(
                error=(
                    f"could not read the file: no such file at '{path}'. "
                    "Provide the current path of an existing file."
                ),
            )
        if path.is_dir():
            return TextReadResult(
                error=(
                    f"could not read '{path}': it is a directory, not a file. "
                    "Provide a file path, or use list_directory for this directory."
                ),
            )
        file_size = self._safe_file_size(path)
        if file_size > self.max_file_bytes:
            return TextReadResult(
                file_size=file_size,
                error=(
                    f"File is too large to read: {file_size} bytes "
                    f"exceeds the {self.max_file_bytes} byte limit. "
                    "read_file cannot open files larger than 5 MB, even with "
                    "pagination. Use search_content with a known text pattern to "
                    "locate matching lines, or "
                    "read a smaller exported or sampled copy of the file."
                ),
            )

        if self._is_likely_binary(path):
            return TextReadResult(
                file_size=self._safe_file_size(path),
                error=(
                    "Binary file cannot be displayed as text (detected by extension or NUL "
                    "bytes). Use a dedicated binary viewer or open it outside the agent; "
                    "reading it with read_file will always fail."
                ),
            )

        offset, limit = self._normalize_read_pagination(offset, limit)
        selected: list[tuple[int, str]] = []
        next_offset = None
        end_line = None
        truncated = False
        total_lines = 0
        current_chars = 0
        try:
            with path.open("r", encoding="utf-8", errors="strict") as file:
                # 整文件扫到底：页头要报文件总行数，而文件已限制在 5 MB 内，多扫一遍的代价
                # 远小于模型另起一次调用去数行数。本页选够之后只计数、不再渲染。
                for line_number, raw_line in enumerate(file, start=1):
                    total_lines = line_number
                    if line_number < offset or next_offset is not None:
                        continue
                    if len(selected) >= limit:
                        next_offset = line_number
                        continue

                    line = raw_line.rstrip("\r\n")
                    if line_number == 1 and line.startswith(self.utf8_bom):
                        line = line[len(self.utf8_bom) :]

                    rendered = self._render_line(line_number, line)
                    addition = len(rendered) + (1 if selected else 0)
                    if current_chars + addition > self.max_content_chars:
                        next_offset = line_number
                        truncated = True
                        continue

                    selected.append((line_number, line))
                    end_line = line_number
                    current_chars += addition
        except UnicodeDecodeError:
            return TextReadResult(
                error=(
                    "File is not valid UTF-8 and cannot be read as text. It may use a "
                    "different encoding (e.g. GBK on Windows) or be a binary file. "
                    "Re-save it as UTF-8, or use a tool that supports its encoding."
                ),
            )
        except OSError as exc:
            return TextReadResult(
                error=(
                    f"{os_error_message(exc, 'read the file')}. "
                    "Make the file readable, then call read_file again."
                ),
            )

        return TextReadResult(
            content=self._page_content(
                selected,
                offset=offset,
                total_lines=total_lines,
                next_offset=next_offset,
                truncated=truncated,
            ),
            total_lines=total_lines,
            file_size=self._safe_file_size(path),
            next_offset=next_offset,
            end_line=end_line,
            truncated=truncated,
        )

    def _page_content(
        self,
        selected: list[tuple[int, str]],
        *,
        offset: int,
        total_lines: int,
        next_offset: int | None,
        truncated: bool,
    ) -> str:
        """组装一页的模型可见文本：页头 ``[lines a-b of N]``、带行号正文、页尾。

        页尾只有两种：``[end of file]`` 或下一段的 offset（因字符预算停下时注明原因，模型据此
        知道本页没到 ``limit`` 不是文件短）。空文件与 offset 越界各给一行事实代替正文，避免
        空字符串让模型分不清「文件空」与「读漏了」。
        """

        if total_lines == 0:
            return "[empty file]\n[end of file]"
        if not selected:
            return (
                f"[offset {offset} is past the end: the file has {total_lines} lines]\n"
                "[end of file]"
            )
        first, _ = selected[0]
        last, _ = selected[-1]
        header = f"[lines {first}-{last} of {total_lines}]"
        if next_offset is None:
            footer = "[end of file]"
        elif truncated:
            footer = f"[stopped at the character budget; continue with offset={next_offset}]"
        else:
            footer = f"[continue with offset={next_offset}]"
        return f"{header}\n{self._line_numbered_content(selected)}\n{footer}"

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

    return ReadFileTool().to_definition_if_avaliable()
