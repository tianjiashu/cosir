"""文件名搜索引擎（find files）。

按 glob 模式匹配文件名/相对路径，结果按修改时间降序（最近编辑在前）排序，支持
offset/limit 分页，对齐 Hermes ``search_files(target='files')`` 语义。

设计边界：
- 只做文件名匹配、排序、分页与格式化输出，不关心工具权限。
- 不依赖外部 find/rg 命令，跨平台零重型依赖。
"""

import fnmatch
from pathlib import Path

from app.core.tools.tool_handler.search.error_prefixes import PATH_NOT_FOUND_PREFIX
from app.core.tools.tool_handler.search.file_walker import iter_files, to_relative

DEFAULT_BUDGET = 20_000
DEFAULT_LIMIT = 50


def _mtime_of(file_path: Path) -> float:
    """读取文件修改时间，失败时返回 0.0 以便稳定排序。

    参数:
        file_path: 目标文件。

    返回:
        修改时间（epoch 秒）；stat 失败时为 0.0。

    异常:
        不向上抛出（OSError 归一为 0.0）。

    副作用:
        无（只读元数据）。
    """

    try:
        return file_path.stat().st_mtime
    except OSError:
        return 0.0


def search_filenames(
    root: Path,
    pattern: str,
    path: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    budget: int = DEFAULT_BUDGET,
) -> tuple[str, int, list[str]]:
    """在项目内按 glob 模式查找文件，按修改时间降序返回分页结果。

    参数:
        root: 项目根目录。
        pattern: 文件名 glob 模式（如 ``*.py``、``*config*``）；裸文件名（不含
            ``/`` 且不以 ``*`` 开头）自动包装为 ``*<name>`` 以匹配任意前缀，
            对齐 Hermes ``rg --files -g`` 的处理。
        path: 可选的基于 root 的子目录（仅在该范围内搜索）。
        limit: 单页最大结果条数（默认 50）。
        offset: 跳过前 N 条结果用于分页（默认 0）。
        budget: 输出字符预算，超出截断并追加提示。

    返回:
        ``(output, total, items)`` 三元组：``output`` 为换行分隔的匹配相对路径字符串
        （mtime 降序；结果被分页截断时追加 ``offset`` 续读提示；搜索路径不存在时
        为 ``Path not found:`` 前缀错误）；``total`` 为分页前的匹配文件总数
        （错误时为 0），供调用方回传结构化命中数；``items`` 为与 ``output`` 同序的
        相对路径列表，供前端 list 布局消费。

    异常:
        不向上抛出遍历异常。

    副作用:
        无（只读目录结构与文件元数据）。
    """

    base = Path(root)
    if path:
        base = base / path
    if not base.exists() or not base.is_dir():
        return f"{PATH_NOT_FOUND_PREFIX} {base}", 0, []

    wrap_bare_name = "/" not in pattern and not pattern.startswith("*")
    glob_pattern = f"*{pattern}" if wrap_bare_name else pattern

    matched: list[Path] = []
    if "/" in glob_pattern:
        # 含路径分隔的 glob 对相对路径整体匹配，无法用文件名级预筛。
        for file_path in iter_files(base, None):
            if fnmatch.fnmatchcase(to_relative(base, file_path), glob_pattern):
                matched.append(file_path)
    else:
        # 文件名级 glob 直接复用 iter_files 的 file_glob 预筛（Path.match）。
        matched = list(iter_files(base, glob_pattern))

    matched.sort(key=_mtime_of, reverse=True)
    total = len(matched)
    page = matched[offset : offset + limit]
    items = [to_relative(base, f) for f in page]

    output = "\n".join(items)
    if budget and len(output) > budget:
        output = output[:budget] + "\n... [output truncated by budget]"
    if total > offset + limit:
        output += (
            f"\n\n[Hint: Results truncated ({total} total). "
            f"Use offset={offset + limit} to see more, or narrow the pattern.]"
        )
    return output, total, items
