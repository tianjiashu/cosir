"""搜索引擎共享的目录遍历原语。

本模块只承载递归文件遍历：跳过忽略目录、按可选 glob 过滤文件名，供内容搜索与
文件名搜索两个引擎复用（消除此前两处重复实现）。

设计边界：
- 只做遍历与过滤，不做匹配、排序、分页与输出格式化。
- 不依赖外部命令，跨平台零重型依赖。
"""

import os
from collections.abc import Iterator
from pathlib import Path

from app.tools.tool_handler.security.windows_reparse_point import (
    is_windows_directory_reparse_point,
)

IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".coding-agent",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        ".venv",
        "venv",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "target",
        "out",
        ".idea",
        ".vscode",
        ".tox",
        ".eggs",
        "site-packages",
    }
)


def iter_files(base: Path, file_glob: str | None = None) -> Iterator[Path]:
    """递归遍历目录下的文件，跳过忽略目录并按 glob 过滤。

    参数:
        capability: 遍历根目录。
        file_glob: 可选的文件名 glob 模式（如 ``*.py``）；为 None 表示不过滤。

    返回:
        文件路径生成器。

    异常:
        不向上抛出遍历异常（权限错误由调用方读取时处理）。

    副作用:
        无（只读目录结构）。
    """

    try:
        with os.scandir(base) as entries:
            for raw_entry in entries:
                entry = Path(raw_entry.path)
                try:
                    if raw_entry.is_symlink() or is_windows_directory_reparse_point(entry):
                        # 与 rg 默认语义一致：不跟随 symlink 或 Windows junction。
                        continue
                    if raw_entry.is_dir(follow_symlinks=False):
                        if entry.name in IGNORED_DIRS:
                            continue
                        yield from iter_files(entry, file_glob)
                    elif raw_entry.is_file(follow_symlinks=False):
                        if file_glob is None or entry.match(file_glob):
                            yield entry
                except OSError:
                    continue
    except OSError:
        return


def to_relative(base: Path, file_path: Path) -> str:
    """把文件绝对路径转为对 capability 的相对路径（统一用 ``/`` 分隔）。

    参数:
        capability: 基准目录。
        file_path: 目标文件。

    返回:
        POSIX 风格相对路径字符串。

    异常:
        ValueError: 当 ``file_path`` 不在 ``capability`` 之下（调用方保证不发生）。

    副作用:
        无。
    """

    return str(file_path.relative_to(base)).replace("\\", "/")
