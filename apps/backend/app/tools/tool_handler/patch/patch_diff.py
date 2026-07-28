"""patch 应用结果的 unified diff 回显与统计。

把 apply 阶段捕获的 before/after 内容投影为 unified diff 文本与结构化 diff 统计，
供 ``patch`` 工具的成功观察回显（``content`` 放 diff 文本、``data`` 放 ``diff_stats``）。

设计边界：
- 只做 diff 投影，不读写文件、不关心工具权限。
- 复用标准库 ``difflib.unified_diff``，不重写差异算法。
- 与 Hermes ``file_operations._unified_diff`` / ``patch_parser.py:509-636`` 行为对齐。
"""

import difflib
from dataclasses import dataclass


@dataclass(frozen=True)
class FileDiffResult:
    """单个文件应用前后的内容快照与状态。

    参数:
        path: 文件相对路径（Move 时为源路径）。
        status: 变更状态，取值 ``modified`` / ``added`` / ``deleted`` / ``moved``。
        before: 应用前的内容（Add 传空字符串）。
        after: 应用后的内容（Delete 传空字符串）。
        new_path: Move 的目标路径；其它状态为 None。

    返回:
        无。

    异常:
        无。

    副作用:
        无（frozen dataclass，不可变）。
    """

    path: str
    status: str
    before: str
    after: str
    new_path: str | None = None


def _to_diff_lines(text: str) -> list[str]:
    """把文本切成逐行列表，并保证每行以换行结尾。

    参数:
        text: 原始文本（可能无结尾换行）。

    返回:
        每行以 ``\\n`` 结尾的列表，供 ``difflib.unified_diff`` 产出正确的逐行格式；
        空文本返回空列表。

    异常:
        无。

    副作用:
        无。
    """

    lines = text.splitlines(keepends=True)
    return [line if line.endswith("\n") else line + "\n" for line in lines]


def format_unified_diff(old_text: str, new_text: str, path: str) -> str:
    """生成单文件的 unified diff 文本。

    参数:
        old_text: 应用前的内容（Add 传空字符串）。
        new_text: 应用后的内容（Delete 传空字符串）。
        path: 文件相对路径，用于 diff 头 ``a/{path}`` / ``b/{path}``。

    返回:
        以换行连接的 unified diff；当 ``old_text == new_text``（无文本变化）时返回
        ``"(no textual change)"``，与 Hermes ``_unified_diff`` 占位一致，确保模型
        行为一致。

    异常:
        无。

    副作用:
        无。
    """

    if old_text == new_text:
        return "(no textual change)"
    return "".join(
        difflib.unified_diff(
            _to_diff_lines(old_text),
            _to_diff_lines(new_text),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def format_patch_diff(results: list[FileDiffResult]) -> str:
    """把多个文件的 diff 快照拼成统一回显文本。

    参数:
        results: 各文件的 before/after 快照与状态（含 Move 的 ``new_path``）。

    返回:
        以换行连接的 unified diff 文本；Move 以 ``# Moved: src -> dst`` 标记行呈现
        （不做逐行 diff，与 Hermes 一致）。

    异常:
        无。

    副作用:
        无。
    """

    parts: list[str] = []
    for result in results:
        if result.status == "moved":
            parts.append(f"# Moved: {result.path} -> {result.new_path}")
        else:
            parts.append(format_unified_diff(result.before, result.after, result.path))
    return "\n".join(parts)


def _count_added(diff: str) -> int:
    """统计 unified diff 中新增行数（不含 ``+++`` 头与 ``@@`` 提示）。"""

    return sum(
        1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
    )


def _count_removed(diff: str) -> int:
    """统计 unified diff 中删除行数（不含 ``---`` 头与 ``@@`` 提示）。"""

    return sum(
        1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")
    )


def build_diff_stats(results: list[FileDiffResult]) -> dict:
    """汇总多个文件的 diff 统计。

    参数:
        results: 各文件的 before/after 快照与状态。

    返回:
        结构化统计字典：``total_files`` / ``total_insertions`` / ``total_deletions`` /
        ``files[]``（``path`` / ``status`` / ``insertions`` / ``deletions`` /
        ``new_path``）。``insertions`` 计 ``+`` 行数，``deletions`` 计 ``-`` 行数
        （均不含 ``@@`` 与上下文行）；Move 计 0/0。

    异常:
        无。

    副作用:
        无。
    """

    files: list[dict] = []
    total_insertions = 0
    total_deletions = 0
    for result in results:
        if result.status == "moved":
            insertions = 0
            deletions = 0
        else:
            diff = format_unified_diff(result.before, result.after, result.path)
            insertions = _count_added(diff)
            deletions = _count_removed(diff)
        files.append(
            {
                "path": result.path,
                "status": result.status,
                "insertions": insertions,
                "deletions": deletions,
                "new_path": result.new_path,
            }
        )
        total_insertions += insertions
        total_deletions += deletions
    return {
        "total_files": len(results),
        "total_insertions": total_insertions,
        "total_deletions": total_deletions,
        "files": files,
    }
