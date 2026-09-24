"""搜索引擎共享的目录遍历原语。

本模块只承载递归文件遍历：按调用方给出的忽略规则跳过目录、按可选 glob 过滤文件名，供内容
搜索与文件名搜索两个引擎复用（消除此前两处重复实现）。

设计边界：
- 只做遍历与过滤，不做匹配、排序、分页与输出格式化。
- 忽略规则由调用方注入（:class:`IgnoreRules`）：本模块不读配置文件、不知道 workspace 位置，
  未注入时回退 :func:`ignore_rules.default_ignore_rules`；持有 workspace 的调用方应传
  ``load_ignore_rules(workspace_root)``。
- 不依赖外部命令，跨平台零重型依赖。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from app.core.tools.tool_handler.search.ignore_rules import IgnoreRules, default_ignore_rules
from app.core.tools.tool_handler.security.windows_reparse_point import (
    is_windows_directory_reparse_point,
)


def iter_files(
    base: Path,
    file_glob: str | None = None,
    *,
    rules: IgnoreRules | None = None,
) -> Iterator[Path]:
    """递归遍历目录下的文件，按忽略规则跳过目录并按 glob 过滤文件名。

    参数:
        base: 遍历根目录。
        file_glob: 可选的文件名 glob 模式（如 ``*.py``）；为 None 表示不过滤。
        rules: 目录忽略规则；为 None 时使用内置默认规则（不含 workspace 的 ``.fileignore``
            配置）。持有 workspace 上下文的调用方应传入 ``load_ignore_rules(workspace_root)``。

    返回:
        文件路径生成器（不保证顺序）。

    异常:
        不向上抛出遍历异常（权限错误由调用方读取时处理）。

    副作用:
        无（只读目录结构）。
    """

    active_rules = rules if rules is not None else default_ignore_rules()
    yield from _walk(base, file_glob, active_rules)


def _walk(base: Path, file_glob: str | None, rules: IgnoreRules) -> Iterator[Path]:
    """递归遍历一层目录；规则对象在整次遍历中复用，避免逐层重复读取规则文件。

    参数:
        base: 当前层目录。
        file_glob: 文件名 glob 过滤模式；None 表示不过滤。
        rules: 本次遍历使用的忽略规则。

    返回:
        当前子树下的文件路径生成器（不保证顺序）。

    异常:
        不向上抛出：目录不可读时返回空结果。

    副作用:
        无（只读目录结构）。
    """

    try:
        with os.scandir(base) as entries:
            for raw_entry in entries:
                yield from _visit_entry(Path(raw_entry.path), raw_entry, file_glob, rules)
    except OSError:
        return


def _visit_entry(
    entry: Path,
    raw_entry: os.DirEntry[str],
    file_glob: str | None,
    rules: IgnoreRules,
) -> Iterator[Path]:
    """判定单个目录项：symlink / junction 跳过、目录按规则跳过、文件按 glob 过滤。

    参数:
        entry: 目录项路径。
        raw_entry: ``os.scandir`` 返回的原始目录项（复用其类型查询，避免重复 stat）。
        file_glob: 文件名 glob 过滤模式；None 表示不过滤。
        rules: 本次遍历使用的忽略规则。

    返回:
        命中的文件路径生成器；目录命中忽略规则时不产出任何项（整棵子树被跳过）。

    异常:
        不向上抛出：单项元数据查询失败（``OSError``）时跳过该项。

    副作用:
        无（只读目录项元数据）。
    """

    try:
        if raw_entry.is_symlink() or is_windows_directory_reparse_point(entry):
            # 与 rg 默认语义一致：不跟随 symlink 或 Windows junction。
            return
        if raw_entry.is_dir(follow_symlinks=False):
            if rules.ignores_dir(entry):
                return
            yield from _walk(entry, file_glob, rules)
        elif raw_entry.is_file(follow_symlinks=False):
            if file_glob is None or entry.match(file_glob):
                yield entry
    except OSError:
        return


def to_relative(base: Path, file_path: Path) -> str:
    """把文件绝对路径转为相对 ``base`` 的路径（统一用 ``/`` 分隔）。

    参数:
        base: 基准目录。
        file_path: 目标文件。

    返回:
        POSIX 风格相对路径字符串。

    异常:
        ValueError: 当 ``file_path`` 不在 ``base`` 之下（调用方保证不发生）。

    副作用:
        无。
    """

    return str(file_path.relative_to(base)).replace("\\", "/")
