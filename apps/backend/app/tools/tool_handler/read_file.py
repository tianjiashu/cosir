"""read_file 工具实现。

本模块只承载 read_file 这一个工具。为了符合“一个工具一个类”的约定，
路径安全、文本读取、分页、二进制判断、行号渲染和工具定义适配都封装在
``ReadFileTool`` 内部。

设计边界：
- 参数校验模型放在 ``app.tools.tool_models.ReadFileArgs``，由 Pydantic 负责。
- 对外注册仍通过 ``build_read_file_definition`` 返回 ``ToolDefinition``，暂不改变注册逻辑。
- 工具执行只读文件系统，不写入任何文件，不执行 shell 命令。
"""

from pathlib import Path
from typing import ClassVar

from app.tools.schemas import ToolDefinition, ToolDisplayHints, ToolObservation
from app.tools.tool_models import ReadFileArgs
from app.tools.tool_models.text_read_result import TextReadResult


class ReadFileTool:
    """读取项目内文本文件的工具类。

    这个类是 read_file 的唯一实现载体。它把工具元信息、执行入口、路径安全、
    文本分页和输出格式化放在同一个类中，便于后续按“一个工具一个类”的方式
    继续扩展其它工具。

    参数:
        project_root: 当前工具允许访问的项目根目录。所有读取请求都必须解析到该
            目录内部，否则返回 ``path_escape`` 错误。

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
        "cat/head/tail in terminal. Output format: 'LINE_NUM|CONTENT'. Use offset "
        "and limit for large files. Reads exceeding about 100K characters are "
        "truncated on a line boundary and return a next_offset; continue with "
        "offset to read the rest. NOTE: Cannot read images or other binary files."
    )
    permission = "safe_read"
    required_params = ("path",)
    args_model = ReadFileArgs
    timeout_seconds = 10.0
    risk_level = "low"

    windows_device_names: ClassVar[set[str]] = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "COM\u00b9",
        "COM\u00b2",
        "COM\u00b3",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
        "LPT\u00b9",
        "LPT\u00b2",
        "LPT\u00b3",
    }
    posix_blocked_device_paths: ClassVar[set[str]] = {
        "/dev/zero",
        "/dev/random",
        "/dev/urandom",
        "/dev/full",
        "/dev/stdin",
        "/dev/tty",
        "/dev/console",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/fd/0",
        "/dev/fd/1",
        "/dev/fd/2",
    }
    proc_blocked_suffixes = (
        "/fd/0",
        "/fd/1",
        "/fd/2",
        "/environ",
        "/cmdline",
        "/maps",
        "/smaps",
        "/smaps_rollup",
        "/numa_maps",
        "/mem",
        "/auxv",
        "/pagemap",
    )
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

    def __init__(self, project_root: str | Path) -> None:
        """初始化 read_file 工具实例。

        参数:
            project_root: read_file 允许访问的项目根目录。

        返回:
            无。

        异常:
            无。

        副作用:
            仅保存 ``project_root``，不执行文件系统读取。
        """

        self.project_root = Path(project_root)

    def execute(self, path: str, offset: int = 1, limit: int = 500) -> ToolObservation:
        """读取项目目录内的文本文件，并返回适合模型消费的观测结果。

        参数:
            path: 用户或模型请求读取的文件路径。可以是相对路径，也可以是解析后仍位于
                ``project_root`` 内的绝对路径。
            offset: 从第几行开始读取，1 表示第一行。
            limit: 最多返回多少行。参数模型会限制最大值，内部也会再次归一化。

        返回:
            ``ToolObservation``。成功时 ``content`` 包含 ``LINE_NUM|CONTENT`` 格式的
            带行号文本；失败时 ``status`` 为 ``error``，并通过 ``reason`` 提供稳定分类。

        异常:
            不主动向上抛出异常。文件不存在、路径逃逸、二进制文件和读取失败都会被转换成
            结构化 ``ToolObservation``。

        副作用:
            只读文件系统，不写入任何文件。
        """

        device_error = self._blocked_device_reason(path)
        if device_error:
            return self._error(device_error, "blocked_device")

        resolved, error = self._resolve_project_path(path)
        if resolved is None:
            return self._error(error, "path_escape")

        device_error = self._blocked_device_reason(path, resolved)
        if device_error:
            return self._error(device_error, "blocked_device")

        result = self._read_text_page(resolved, offset, limit)
        if result.error:
            return self._error(result.error, result.reason, result.retryable)
        return ToolObservation(
            tool_name=self.name,
            status="success",
            content=result.content,
            permission=self.permission,
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
            required_params=self.required_params,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            display=ToolDisplayHints(
                verb="读取",
                icon="eye",
                summary_template="{path} · L{start}-L{end}",
                detail_keys=("path", "offset", "limit"),
                click_action="open_file:{path}",
            ),
        )

    def _resolve_project_path(self, path: str) -> tuple[Path | None, str]:
        """解析用户路径，并确认最终路径仍在项目根目录内。

        参数:
            path: 模型传入的路径字符串。

        返回:
            ``(resolved_path, "")`` 表示成功；``(None, error)`` 表示路径非法。

        异常:
            不向上抛出。解析失败会被转换成错误字符串。

        副作用:
            无。
        """

        if not isinstance(path, str) or not path.strip():
            return None, "path must be a non-empty string"

        root = self.project_root.resolve()
        raw = Path(path)
        target = raw if raw.is_absolute() else root / raw
        try:
            resolved = target.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            return None, f"path escapes project root: {path} ({exc})"
        return resolved, ""

    def _blocked_device_reason(self, path: str, resolved: Path | None = None) -> str:
        """判断路径是否指向应当禁止读取的系统设备或敏感伪文件。

        参数:
            path: 原始路径字符串。
            resolved: 可选的归一化路径，用于二次检查符号链接解析后的目标。

        返回:
            空字符串表示允许继续；非空字符串表示命中禁止路径。

        异常:
            无。

        副作用:
            无。
        """

        if self._is_blocked_posix_path(path):
            return f"blocked device path: {path}"
        if resolved is not None and self._is_blocked_posix_path(resolved.as_posix()):
            return f"blocked device path: {path}"

        if self._windows_device_name(path):
            return f"blocked device path: {path}"
        if resolved is not None and self._windows_device_name(str(resolved)):
            return f"blocked device path: {path}"
        return ""

    def _is_blocked_posix_path(self, path: str) -> bool:
        """判断路径是否命中 POSIX 设备或 Linux procfs 敏感路径。

        参数:
            path: 待检查路径。

        返回:
            True 表示必须拒绝读取；False 表示未命中该类规则。

        异常:
            无。

        副作用:
            无。
        """

        normalized = str(path).replace("\\", "/").lower().rstrip("/")
        if normalized in self.posix_blocked_device_paths:
            return True
        return normalized.startswith("/proc/") and normalized.endswith(self.proc_blocked_suffixes)

    def _windows_device_name(self, path: str) -> str:
        """返回命中的 Windows 设备名；没有命中时返回空字符串。

        参数:
            path: 待检查路径。

        返回:
            命中的设备名，例如 ``NUL``；未命中时返回空字符串。

        异常:
            无。

        副作用:
            无。
        """

        normalized = str(path).replace("\\", "/")
        leaf = normalized.rsplit("/", 1)[-1].rstrip(" .")
        stem = leaf.split(".", 1)[0].rstrip(" .").upper()
        return stem if stem in self.windows_device_names else ""

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
            不主动向上抛出文件系统异常。读取失败会返回 ``reason='read_failed'``。

        副作用:
            只读文件系统，不写入任何文件。
        """

        if not path.exists():
            return TextReadResult(error=f"file not found: {path}", reason="not_found")
        if path.is_dir():
            return TextReadResult(error=f"path is a directory: {path}", reason="is_directory")
        if self._is_likely_binary(path):
            return TextReadResult(
                file_size=self._safe_file_size(path),
                error="Binary file cannot be displayed as text.",
                reason="binary_file",
            )

        offset, limit = self._normalize_read_pagination(offset, limit)
        selected: list[tuple[int, str]] = []
        next_offset = None
        hint = ""
        total_lines = 0
        current_chars = 0
        try:
            with path.open("r", encoding="utf-8", errors="replace") as file:
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
        except OSError as exc:
            return TextReadResult(error=f"read failed: {exc}", reason="read_failed", retryable=True)

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

    def _error(self, error: str, reason: str, retryable: bool = False) -> ToolObservation:
        """构造 read_file 的标准错误观测。

        参数:
            error: 面向开发者和模型的错误文本。
            reason: 稳定错误分类，便于上层 runtime、测试和 UI 判断。
            retryable: 该错误是否适合稍后重试。

        返回:
            ``status`` 为 ``error`` 的 ``ToolObservation``。

        异常:
            无。

        副作用:
            无。
        """

        return ToolObservation(
            tool_name=self.name,
            status="error",
            content="",
            error=error,
            reason=reason,
            retryable=retryable,
            permission=self.permission,
        )


def build_read_file_definition(project_root: str | Path) -> ToolDefinition:
    """构造绑定到指定项目根目录的 read_file 工具定义。

    参数:
        project_root: read_file 允许访问的项目根目录。

    返回:
        ``ToolDefinition``，供 ``ToolRegistry`` 注册。

    异常:
        无。

    副作用:
        创建 ``ReadFileTool`` 实例和定义对象，不执行文件读取。
    """

    return ReadFileTool(project_root).to_definition()
