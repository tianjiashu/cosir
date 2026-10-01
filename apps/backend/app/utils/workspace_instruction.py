"""workspace ``AGENTS.md`` 定位器。

运行时提示词构建和 workspace 配置页面必须共享同一套文件选择规则。本模块只定位文件，不读取
正文、不创建文件；候选文件名、忽略目录、扫描深度和同层排序规则集中在此处维护。
"""

from __future__ import annotations

import os
from collections import deque
from pathlib import Path

WORKSPACE_INSTRUCTION_FILE_NAME = "AGENTS.md"
WORKSPACE_INSTRUCTION_MAX_DEPTH = 4
IGNORED_INSTRUCTION_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "dist",
        "build",
        ".idea",
        ".vscode",
        ".svn",
        ".hg",
    }
)


def find_workspace_instruction_file(root: str | Path) -> tuple[Path, Path] | None:
    """按运行时规则定位 workspace 当前有效的 ``AGENTS.md``。

    参数:
        root: workspace 根目录。

    返回:
        ``(相对路径, 绝对路径)``；没有候选文件或目录不可读时返回 ``None``。

    异常:
        无；路径解析、权限和目录读取失败均视为没有命中。

    副作用:
        只遍历目录，不读取文件正文，不跟随符号链接，不修改文件系统。
    """

    target = WORKSPACE_INSTRUCTION_FILE_NAME.lower()
    try:
        workspace_root = Path(root).resolve()
    except OSError:
        return None
    if not workspace_root.is_dir():
        return None

    best_key: tuple[int, str] | None = None
    best_relative: Path | None = None
    best_absolute: Path | None = None
    queue: deque[tuple[Path, int]] = deque([(workspace_root, 0)])

    while queue:
        for _ in range(len(queue)):
            directory, depth = queue.popleft()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if entry.name.lower() == target:
                            try:
                                relative = Path(entry.path).relative_to(workspace_root)
                            except ValueError:
                                continue
                            key = (len(relative.parts), relative.as_posix())
                            if best_key is None or key < best_key:
                                best_key = key
                                best_relative = relative
                                best_absolute = Path(entry.path)
                        elif (
                            depth + 1 <= WORKSPACE_INSTRUCTION_MAX_DEPTH
                            and entry.name not in IGNORED_INSTRUCTION_DIRS
                            and entry.is_dir(follow_symlinks=False)
                        ):
                            queue.append((Path(entry.path), depth + 1))
            except (PermissionError, OSError):
                continue
        if best_key is not None:
            break

    if best_relative is None or best_absolute is None:
        return None
    return best_relative, best_absolute
