"""内容搜索引擎（正则内容搜索）。

纯 Python 递归遍历 + 正则匹配，支持忽略目录、file_glob 文件名过滤、上下文行、
三种输出模式（content / files_only / count）、offset/limit 行分页与字符预算截断，
对齐 Hermes ``search_files(target='content')`` 语义。

设计边界：
- 只做内容搜索与格式化输出，不关心工具权限。
- 不依赖外部命令，跨平台零重型依赖。
- 结果格式化交由本模块完成，调用方直接把返回值作为观察内容。
"""

import re
from pathlib import Path

from app.tools.tool_handler.search.error_prefixes import (
    INVALID_REGEX_PREFIX,
    PATH_NOT_FOUND_PREFIX,
)
from app.tools.tool_handler.search.file_walker import iter_files, to_relative

DEFAULT_BUDGET = 20_000
DEFAULT_LIMIT = 50


def search_content(
    root: Path,
    pattern: str,
    path: str | None = None,
    file_glob: str | None = None,
    output_mode: str = "content",
    context: int = 0,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    budget: int = DEFAULT_BUDGET,
) -> str:
    """在项目内递归搜索正则匹配的内容并以紧凑格式返回分页结果。

    参数:
        root: 项目根目录。
        pattern: 正则表达式模式。
        path: 可选的基于 root 的子目录（仅在该范围内搜索）。
        file_glob: 可选的文件名 glob 过滤（如 ``*.py``）。
        output_mode: ``content``（命中行 + 上下文）/ ``files_only``（仅文件路径）/
            ``count``（每文件命中数）。
        context: 命中行前后的上下文行数。
        limit: 单页最大输出行数（默认 50）。
        offset: 跳过前 N 行输出用于分页（默认 0）。
        budget: 输出字符预算，超出截断并追加提示。

    返回:
        格式化搜索结果字符串；结果被分页截断时追加 ``offset`` 续读提示；正则非法时
        返回 ``invalid regex:`` 前缀错误，搜索路径不存在时返回 ``Path not found:``
        前缀错误。

    异常:
        不向上抛出遍历/读取异常（跳过不可读文件）。

    副作用:
        仅读取文件内容；不修改文件系统。
    """

    base = Path(root)
    if path:
        base = base / path
    if not base.exists() or not base.is_dir():
        return f"{PATH_NOT_FOUND_PREFIX} {base}"

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"{INVALID_REGEX_PREFIX} {exc}"

    files_mode = output_mode == "files_only"
    count_mode = output_mode == "count"
    output_lines: list[str] = []

    for file_path in iter_files(base, file_glob):
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.split("\n")
        hits = [i for i, line in enumerate(lines, start=1) if regex.search(line)]
        if not hits:
            continue
        if files_mode:
            output_lines.append(to_relative(base, file_path))
            continue
        if count_mode:
            output_lines.append(f"{to_relative(base, file_path)}: {len(hits)}")
            continue
        for i in hits:
            start = max(1, i - context)
            end = min(len(lines), i + context)
            for ln in range(start, end + 1):
                marker = ">" if ln == i else " "
                output_lines.append(f"{to_relative(base, file_path)}:{ln}:{marker}{lines[ln - 1]}")

    total = len(output_lines)
    page = output_lines[offset : offset + limit]

    output = "\n".join(page)
    if budget and len(output) > budget:
        output = output[:budget] + "\n... [output truncated by budget]"
    if total > offset + limit:
        output += (
            f"\n\n[Hint: Results truncated ({total} total lines). "
            f"Use offset={offset + limit} to see more, or narrow with a more "
            "specific pattern or file_glob.]"
        )
    return output
