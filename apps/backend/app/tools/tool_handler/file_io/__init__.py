"""文件原子写入与格式检测包。

本包承载文件落盘的安全基础设施：原子写（避免半写文件）、BOM/CRLF 保留、
以及把带行号前缀的输出误回写的内容识别为行号污染。
"""

from app.tools.tool_handler.file_io.atomic_write import (
    atomic_write_text,
    detect_bom,
    detect_line_ending,
    looks_like_line_numbered,
)

__all__ = [
    "atomic_write_text",
    "detect_bom",
    "detect_line_ending",
    "looks_like_line_numbered",
]
