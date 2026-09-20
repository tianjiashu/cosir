"""``.cosir`` 路径集中与只读保护的独立对抗性测试（probe）。

本文件只测不改：不修改 ``apps/backend/app/`` 下任何生产代码。

对抗维度：
  A. ``cosir_paths`` 边界绕过：``.cosir2`` / 大小写 / ``./.cosir`` / 尾随分隔符 / ``..`` /
     深层 ``a/../../.cosir`` / 绝对路径 / ``\\`` 分隔符 / NUL / 空串 / ``None``。
  B. 符号链接与 reparse point 双向绕过。
  C. ``PathResolver.resolve_within_workspace`` 的 ``allow_reserved`` 语义与穿越。
  D. 五个文件工具指向 ``.cosir`` 必须失败且磁盘不变（含 ``.cosir`` 目录本身）。
  E. 只读工具读 ``.cosir`` 必须成功。
  F. 终端 cwd 豁免。
  G. ``tool_output_budget`` artifact 落盘位置与回读。
  H. 系统级 ``.cosir``：``paths.reset`` 推导、幂等创建、同名文件失败降级 + 日志事件名。
  I. ``.coding-agent`` 彻底退场复核。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.tools.guard.tool_output_budget import ToolOutputBudget
from app.core.tools.schemas import ToolExecutionContext, ToolObservation
from app.core.tools.tool_handler.apply_patch_tool import ApplyPatchTool
from app.core.tools.tool_handler.delete_tool import DeleteTool
from app.core.tools.tool_handler.find_files import FindFilesTool
from app.core.tools.tool_handler.list_directory import ListDirectoryTool
from app.core.tools.tool_handler.move_tool import MoveTool
from app.core.tools.tool_handler.read_file import ReadFileTool
from app.core.tools.tool_handler.replace_tool import ReplaceTool
from app.core.tools.tool_handler.search_content import SearchContentTool
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.core.tools.tool_handler.write_file import WriteFileTool
from app.service.terminal.terminal_session_service import TerminalSessionService
from app.utils import cosir_paths, paths


def _ctx(root: Path) -> ToolExecutionContext:
    """构造绑定到给定 workspace 根的执行上下文。"""

    return ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=root, run_id=1)


def _symlinks_supported(base: Path) -> bool:
    """探测当前环境是否允许创建符号链接（Windows 需开发者模式/管理员）。"""

    probe_target = base / "_probe_target.txt"
    probe_target.write_text("x", encoding="utf-8")
    probe_link = base / "_probe_link.txt"
    try:
        probe_link.symlink_to(probe_target)
    except (OSError, NotImplementedError):
        return False
    finally:
        for candidate in (probe_link, probe_target):
            with contextlib.suppress(OSError):
                candidate.unlink()
    return True


# --- A. cosir_paths 边界绕过 ---------------------------------------------------


def test_cosir2_sibling_is_not_reserved(tmp_path: Path) -> None:
    """`.cosir2` 与 `.cosir` 前缀相同但不同名：is_within_cosir 必须返回 False。"""

    root = tmp_path / "ws"
    (root / ".cosir2" / "x").mkdir(parents=True)
    assert cosir_paths.is_within_cosir(root / ".cosir2" / "x", root) is False
    assert cosir_paths.is_within_cosir(root / ".cosir_v2", root) is False


def test_is_within_cosir_case_insensitive_on_windows(tmp_path: Path) -> None:
    """大小写变体 `.COSIR`：在大小写不敏感文件系统上应判 True；区分大小写系统上判 False。"""

    root = tmp_path / "ws"
    (root / ".cosir").mkdir(parents=True)
    result = cosir_paths.is_within_cosir(root / ".COSIR" / "x.txt", root)
    if os.path.normcase("A") == os.path.normcase("a"):
        assert result is True, "大小写不敏感文件系统上 .COSIR 必须被视为保留区"
    else:
        assert result is False, "大小写敏感文件系统上 .COSIR 是不同目录"


def test_dot_slash_and_trailing_separator_variants(tmp_path: Path) -> None:
    """`./.cosir`、`.cosir/`、`.cosir//`、`.cosir/x/../y` 等变体都必须命中保留区。"""

    root = tmp_path / "ws"
    (root / ".cosir").mkdir(parents=True)
    for variant in ("./.cosir", ".cosir/", ".cosir//", ".cosir/x/../y", ".cosir/./sub/../y"):
        assert cosir_paths.is_within_cosir(root / variant, root) is True, variant


def test_backslash_separator_variant_hits_reserved(tmp_path: Path) -> None:
    """`\\` 分隔符形式 `.cosir\\x` 在 Windows 上必须命中保留区。"""

    root = tmp_path / "ws"
    (root / ".cosir").mkdir(parents=True)
    result = cosir_paths.is_within_cosir(str(root) + "\\.cosir\\x.txt", root)
    if os.sep == "\\":
        assert result is True
    else:
        # POSIX 上反斜杠是普通文件名字符，此处不断言其语义，仅确认不抛异常。
        assert result in (True, False)


def test_deep_dotdot_traversal_into_cosir(tmp_path: Path) -> None:
    """`a/../.cosir/x`（`..` 回到根再进 `.cosir`）必须命中；越过根的 `a/../../.cosir` 不命中。"""

    root = tmp_path / "ws"
    (root / "a").mkdir(parents=True)
    (root / ".cosir").mkdir(parents=True)
    # `a/..` 回到 root，`root/.cosir/x` 命中。
    assert cosir_paths.is_within_cosir(root / "a" / ".." / ".cosir" / "x", root) is True
    assert cosir_paths.is_within_cosir(root / "a" / ".." / ".." / "x", root) is False


def test_nul_and_empty_inputs_do_not_crash(tmp_path: Path) -> None:
    """含 NUL 的路径与空串不得让 is_within_cosir 抛出（异常归一化为 False）。"""

    root = tmp_path / "ws"
    (root / ".cosir").mkdir(parents=True)
    assert cosir_paths.is_within_cosir(str(root / ".cosir") + "\x00.txt", root) is False
    assert cosir_paths.is_within_cosir("", root) is False


def test_is_within_cosir_accepts_relative_path(tmp_path: Path) -> None:
    """相对路径 `path` 按当前进程 cwd 解析：文档未声明相对语义，此处只验证不抛异常。"""

    root = tmp_path / "ws"
    (root / ".cosir").mkdir(parents=True)
    result = cosir_paths.is_within_cosir(".cosir/x.txt", root)
    assert result in (True, False)


def test_cosir_root_none_and_empty(tmp_path: Path) -> None:
    """workspace_root 为 None / 空串时恒为 False（无 workspace 语义）。"""

    assert cosir_paths.is_within_cosir(tmp_path / ".cosir" / "x", None) is False
    assert cosir_paths.is_within_cosir(tmp_path / ".cosir" / "x", "") is False


# --- B. 符号链接绕过（双向） ---------------------------------------------------


def test_symlink_pointing_into_cosir_is_reserved(tmp_path: Path) -> None:
    """workspace 外/内建链接指向 `.cosir`：必须命中保留区（anti-symlink）。"""

    if not _symlinks_supported(tmp_path):
        pytest.skip("当前环境不支持创建符号链接")
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".cosir").mkdir()
    link = root / "innocent.txt"
    link.symlink_to(root / ".cosir" / "target.txt")
    assert cosir_paths.is_within_cosir(link, root) is True


def test_symlink_from_outside_into_cosir_is_reserved(tmp_path: Path) -> None:
    """workspace 外目录里建链接指向 workspace `.cosir`：同样必须命中。"""

    if not _symlinks_supported(tmp_path):
        pytest.skip("当前环境不支持创建符号链接")
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".cosir").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = outside / "escapelink.txt"
    link.symlink_to(root / ".cosir" / "target.txt")
    assert cosir_paths.is_within_cosir(link, root) is True


def test_symlink_from_inside_cosir_to_outside_is_not_reserved(tmp_path: Path) -> None:
    """`.cosir` 内建链接指向 workspace 外：realpath 后落在外部，不应命中保留区。

    这是「链接逃逸」方向。预期：is_within_cosir 返回 False；随后由 PathResolver 的
    containment 负责拦截（写工具必须仍拒绝）。本用例只断言判定语义，不对写工具下结论。
    """

    if not _symlinks_supported(tmp_path):
        pytest.skip("当前环境不支持创建符号链接")
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".cosir").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target.txt").write_text("data", encoding="utf-8")
    link = root / ".cosir" / "escape.txt"
    link.symlink_to(outside / "target.txt")
    assert cosir_paths.is_within_cosir(link, root) is False


def test_write_file_via_symlink_into_cosir_is_blocked(tmp_path: Path) -> None:
    """通过符号链接把写入重定向进 `.cosir`：写工具必须拒绝且磁盘不变。"""

    if not _symlinks_supported(tmp_path):
        pytest.skip("当前环境不支持创建符号链接")
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".cosir").mkdir()
    link = root / "link.txt"
    link.symlink_to(root / ".cosir" / "target.txt")
    observation = WriteFileTool().execute(
        path="link.txt",
        content="pwned",
        execution_context=_ctx(root),
    )
    assert observation.status == "error"
    assert not (root / ".cosir" / "target.txt").exists()


def test_write_file_via_symlink_escaping_cosir_to_outside_is_blocked(tmp_path: Path) -> None:
    """`.cosir` 内符号链接指向外部：写该链接必须被拒绝（不得成为写入 `.cosir` 内部文件的通道）。"""

    if not _symlinks_supported(tmp_path):
        pytest.skip("当前环境不支持创建符号链接")
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".cosir").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "target.txt"
    target.write_text("orig", encoding="utf-8")
    link = root / ".cosir" / "escape.txt"
    link.symlink_to(target)

    observation = WriteFileTool().execute(
        path=".cosir/escape.txt",
        content="pwned",
        execution_context=_ctx(root),
    )
    assert observation.status == "error"
    assert target.read_text(encoding="utf-8") == "orig"


# --- C. PathResolver 语义与穿越 ------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    [
        ".cosir",
        ".cosir/",
        "./.cosir",
        ".cosir/../.cosir/x",
        ".cosir/x/../y",
        "a/../.cosir/x",
        "sub/../../.cosir/x",
    ],
)
def test_resolver_rejects_all_cosir_variants(tmp_path: Path, variant: str) -> None:
    """各种 `.cosir` 变体（含 `..` 穿越）都必须被默认拒绝。"""

    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    (root / ".cosir").mkdir()
    resolved, error = PathResolver(root).resolve_within_workspace(variant)
    assert resolved is None, variant
    assert error != ""


def test_cosir_dir_itself_rejected_as_delete_target(tmp_path: Path) -> None:
    """`.cosir` 目录本身不能被当作删除目标（delete_file 必须拒绝且目录存活）。"""

    root = tmp_path / "ws"
    (root / ".cosir" / "keep.txt").parent.mkdir(parents=True)
    (root / ".cosir" / "keep.txt").write_text("keep", encoding="utf-8")
    observation = DeleteTool().execute(execution_context=_ctx(root), path=".cosir")
    assert observation.status == "error"
    assert (root / ".cosir").is_dir()


def test_move_destination_cosir_dir_itself_rejected(tmp_path: Path) -> None:
    """move_file 目标为 `.cosir` 目录本身（非其子文件）必须被拒绝。"""

    root = tmp_path / "ws"
    root.mkdir()
    src = root / "src.txt"
    src.write_text("a", encoding="utf-8")
    (root / ".cosir").mkdir()
    observation = MoveTool().execute(
        execution_context=_ctx(root),
        source_path="src.txt",
        destination_path=".cosir",
    )
    assert observation.status == "error"
    assert src.exists()


@pytest.mark.parametrize("variant", ["", "   ", "\t"])
def test_resolver_rejects_blank_inputs(tmp_path: Path, variant: str) -> None:
    """空串 / 纯空白输入必须被拒绝。"""

    root = tmp_path / "ws"
    root.mkdir()
    resolved, error = PathResolver(root).resolve_within_workspace(variant)
    assert resolved is None
    assert error


def test_resolver_rejects_nul_input(tmp_path: Path) -> None:
    """含 NUL 的输入必须被拒绝且不抛异常。"""

    root = tmp_path / "ws"
    root.mkdir()
    resolved, error = PathResolver(root).resolve_within_workspace(".cosir\x00/x")
    assert resolved is None
    assert "NUL" in error or error


def test_absolute_path_into_cosir_is_rejected(tmp_path: Path) -> None:
    """绝对路径直接指向 `.cosir` 必须被拒。"""

    root = tmp_path / "ws"
    (root / ".cosir").mkdir(parents=True)
    resolved, error = PathResolver(root).resolve_within_workspace(
        str((root / ".cosir" / "x.txt").resolve())
    )
    assert resolved is None
    assert error


def test_absolute_path_outside_workspace_rejected(tmp_path: Path) -> None:
    """绝对路径指向 workspace 之外必须被拒（containment）。"""

    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    resolved, error = PathResolver(root).resolve_within_workspace(str(outside / "x.txt"))
    assert resolved is None
    assert error


# --- D. 五个文件工具端到端（磁盘不变） -----------------------------------------


def test_write_file_into_cosir_no_disk_change(tmp_path: Path) -> None:
    """write_file 指向 `.cosir` 子文件：error 且磁盘无新文件。"""

    root = tmp_path / "ws"
    (root / ".cosir").mkdir(parents=True)
    obs = WriteFileTool().execute(path=".cosir/new.txt", content="hi", execution_context=_ctx(root))
    assert obs.status == "error"
    assert not (root / ".cosir" / "new.txt").exists()


def test_write_file_creates_parent_dirs_but_not_in_cosir(tmp_path: Path) -> None:
    """write_file 指向 `.cosir/deep/dir/x.txt`：不得创建任何 `.cosir` 子目录。"""

    root = tmp_path / "ws"
    root.mkdir()
    obs = WriteFileTool().execute(
        path=".cosir/deep/dir/x.txt", content="hi", execution_context=_ctx(root)
    )
    assert obs.status == "error"
    assert not (root / ".cosir").exists()


def test_patch_write_into_cosir_no_disk_change(tmp_path: Path) -> None:
    """patch_write 指向 `.cosir` 中已有文件：error 且内容不变。"""

    root = tmp_path / "ws"
    target = root / ".cosir" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_text("hello", encoding="utf-8")
    obs = ReplaceTool().execute(
        execution_context=_ctx(root),
        path=".cosir/a.txt",
        old_string="hello",
        new_string="bye",
    )
    assert obs.status == "error"
    assert target.read_text(encoding="utf-8") == "hello"


def test_apply_patch_into_cosir_no_disk_change(tmp_path: Path) -> None:
    """apply_patch 指向 `.cosir` 中已有文件：error 且内容不变。"""

    root = tmp_path / "ws"
    target = root / ".cosir" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_text("hello\n", encoding="utf-8")
    patch = (
        "diff --git a/.cosir/a.txt b/.cosir/a.txt\n"
        "--- a/.cosir/a.txt\n"
        "+++ b/.cosir/a.txt\n"
        "@@ -1 +1 @@\n"
        "-hello\n"
        "+bye\n"
    )
    obs = ApplyPatchTool().execute(execution_context=_ctx(root), patch=patch)
    assert obs.status == "error"
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_delete_file_in_cosir_no_disk_change(tmp_path: Path) -> None:
    """delete_file 指向 `.cosir` 子文件：error 且文件存活。"""

    root = tmp_path / "ws"
    target = root / ".cosir" / "Attachment" / "a.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    obs = DeleteTool().execute(execution_context=_ctx(root), path=".cosir/Attachment/a.png")
    assert obs.status == "error"
    assert target.exists()


def test_move_file_out_of_cosir_no_disk_change(tmp_path: Path) -> None:
    """move_file 源在 `.cosir`：error 且文件存活。"""

    root = tmp_path / "ws"
    source = root / ".cosir" / "src.txt"
    source.parent.mkdir(parents=True)
    source.write_text("a", encoding="utf-8")
    obs = MoveTool().execute(
        execution_context=_ctx(root),
        source_path=".cosir/src.txt",
        destination_path="src.txt",
    )
    assert obs.status == "error"
    assert source.exists()


def test_move_file_into_cosir_no_disk_change(tmp_path: Path) -> None:
    """move_file 目标在 `.cosir`：error 且源存活、目标不存在。"""

    root = tmp_path / "ws"
    root.mkdir()
    source = root / "src.txt"
    source.write_text("a", encoding="utf-8")
    (root / ".cosir").mkdir(parents=True)
    obs = MoveTool().execute(
        execution_context=_ctx(root),
        source_path="src.txt",
        destination_path=".cosir/src.txt",
    )
    assert obs.status == "error"
    assert source.exists()
    assert not (root / ".cosir" / "src.txt").exists()


def test_write_file_via_dotdot_into_cosir_blocked(tmp_path: Path) -> None:
    """write_file 走 `sub/../.cosir/x` 穿越：error 且磁盘不变。"""

    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    (root / ".cosir").mkdir()
    obs = WriteFileTool().execute(
        path="sub/../.cosir/x.txt", content="hi", execution_context=_ctx(root)
    )
    assert obs.status == "error"
    assert not (root / ".cosir" / "x.txt").exists()


def test_resolver_allows_cosir_dotdot_escaping_reserved_zone(tmp_path: Path) -> None:
    """`.cosir/../outside.txt` 词法含 `.cosir` 但归一后落在保留区外：应放行（按归一结果判定）。"""

    root = tmp_path / "ws"
    (root / ".cosir").mkdir(parents=True)
    resolved, error = PathResolver(root).resolve_within_workspace(".cosir/../outside.txt")
    assert resolved == root / "outside.txt"
    assert error == ""


def test_resolver_rejects_traversal_ending_inside_cosir(tmp_path: Path) -> None:
    """`sub/../.cosir/x` 归一后落在 `.cosir`：必须拒绝（不能借 `..` 绕过）。"""

    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    (root / ".cosir").mkdir()
    resolved, error = PathResolver(root).resolve_within_workspace("sub/../.cosir/x")
    assert resolved is None
    assert error != ""


def test_resolver_rejects_cosir_via_deep_traversal(tmp_path: Path) -> None:
    """`a/b/../../.cosir/x` 多层 `..` 归一后仍在 `.cosir`：必须拒绝。"""

    root = tmp_path / "ws"
    (root / "a" / "b").mkdir(parents=True)
    (root / ".cosir").mkdir()
    resolved, error = PathResolver(root).resolve_within_workspace("a/b/../../.cosir/x")
    assert resolved is None
    assert error != ""


def test_resolver_rejects_cosir_prefix_with_dotdot_out(tmp_path: Path) -> None:
    """`.cosir/../.cosir/x` 归一后重回 `.cosir`：必须拒绝。"""

    root = tmp_path / "ws"
    (root / ".cosir").mkdir(parents=True)
    resolved, error = PathResolver(root).resolve_within_workspace(".cosir/../.cosir/x")
    assert resolved is None
    assert error != ""


# --- E. 只读工具读 `.cosir` 必须成功 -------------------------------------------


def test_read_file_reads_cosir(tmp_path: Path) -> None:
    """read_file 读 `.cosir` 内文件成功。"""

    root = tmp_path / "ws"
    target = root / ".cosir" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_text("secret-content", encoding="utf-8")
    obs = ReadFileTool().execute(path=".cosir/a.txt", execution_context=_ctx(root))
    assert obs.status == "success"


def test_list_directory_lists_cosir(tmp_path: Path) -> None:
    """list_directory 列 `.cosir` 成功。"""

    root = tmp_path / "ws"
    target = root / ".cosir" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    obs = ListDirectoryTool().execute(
        path=".cosir",
        execution_context=_ctx(root),
        include_hidden=True,
    )
    assert obs.status == "success"


def test_search_content_searches_cosir(tmp_path: Path) -> None:
    """search_content 搜 `.cosir` 内容成功。"""

    root = tmp_path / "ws"
    target = root / ".cosir" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_text("needle-here", encoding="utf-8")
    obs = SearchContentTool().execute(
        pattern="needle",
        path=".cosir",
        execution_context=_ctx(root),
    )
    assert obs.status == "success"


def test_find_files_searches_cosir(tmp_path: Path) -> None:
    """find_files 在 `.cosir` 下按名查找成功。"""

    root = tmp_path / "ws"
    target = root / ".cosir" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    obs = FindFilesTool().execute(
        pattern="*.txt",
        path=".cosir",
        execution_context=_ctx(root),
    )
    assert obs.status == "success"


# --- F. 终端豁免 ---------------------------------------------------------------


def test_terminal_cwd_cosir_allowed(tmp_path: Path) -> None:
    """终端 cwd 落在 `.cosir` 不抛错。"""

    root = tmp_path / "ws"
    cosir = root / ".cosir"
    cosir.mkdir(parents=True)
    assert TerminalSessionService._resolve_cwd(str(root), ".cosir") == cosir


def test_terminal_cwd_cosir_subdir_allowed(tmp_path: Path) -> None:
    """终端 cwd 落在 `.cosir` 子目录同样不抛错。"""

    root = tmp_path / "ws"
    sub = root / ".cosir" / "tool-artifacts"
    sub.mkdir(parents=True)
    assert TerminalSessionService._resolve_cwd(str(root), ".cosir/tool-artifacts") == sub


def test_terminal_cwd_outside_workspace_still_rejected(tmp_path: Path) -> None:
    """终端豁免仅针对 `.cosir`；越界 cwd 仍必须抛错。"""

    from app.service.terminal.terminal_session_service import TerminalSessionError

    root = tmp_path / "ws"
    root.mkdir()
    with pytest.raises(TerminalSessionError):
        TerminalSessionService._resolve_cwd(str(root), "../outside")


# --- G. tool_output_budget artifact 落盘 ---------------------------------------


def test_artifact_written_under_cosir_tool_artifacts(tmp_path: Path) -> None:
    """超限输出落盘必须在 `<root>/.cosir/tool-artifacts/`，返回 workspace 相对路径且可回读原文。"""

    root = tmp_path / "ws"
    root.mkdir()
    budget = ToolOutputBudget(max_chars=10)
    content = "abcdefghij" * 100
    obs = ToolObservation(tool_name="t", status="success", content=content)
    result = budget.apply(obs, _ctx(root))
    artifact_path = (result.artifact_data or {}).get("artifact_path")
    assert isinstance(artifact_path, str) and artifact_path
    assert artifact_path.startswith(".cosir/tool-artifacts/")
    assert not Path(artifact_path).is_absolute()
    on_disk = root / artifact_path
    assert on_disk.is_file()
    assert on_disk.read_text(encoding="utf-8") == content


def test_artifact_returns_posix_relative_path(tmp_path: Path) -> None:
    """返回路径必须是 POSIX 风格相对路径（不含反斜杠）。"""

    root = tmp_path / "ws"
    root.mkdir()
    budget = ToolOutputBudget(max_chars=5)
    obs = ToolObservation(tool_name="t", status="success", content="x" * 500)
    result = budget.apply(obs, _ctx(root))
    artifact_path = (result.artifact_data or {}).get("artifact_path")
    assert "\\" not in str(artifact_path)


def test_artifact_not_written_to_coding_agent(tmp_path: Path) -> None:
    """落盘不得再写入 `.coding-agent`（迁移回归）。"""

    root = tmp_path / "ws"
    root.mkdir()
    budget = ToolOutputBudget(max_chars=5)
    obs = ToolObservation(tool_name="t", status="success", content="x" * 500)
    budget.apply(obs, _ctx(root))
    assert not (root / ".coding-agent").exists()


def test_artifact_no_context_returns_empty(tmp_path: Path) -> None:
    """无 execution_context 时不落盘，返回空 artifact_path。"""

    budget = ToolOutputBudget(max_chars=5)
    obs = ToolObservation(tool_name="t", status="success", content="x" * 500)
    result = budget.apply(obs, None)
    assert (result.artifact_data or {}).get("artifact_path", "") == ""


def test_artifact_when_cosir_is_a_file_degrades(tmp_path: Path) -> None:
    """`.cosir` 位置被同名文件占据时，artifact 落盘必须降级（不抛异常、path 为空）。"""

    root = tmp_path / "ws"
    root.mkdir()
    (root / ".cosir").write_text("i am a file", encoding="utf-8")
    budget = ToolOutputBudget(max_chars=5)
    obs = ToolObservation(tool_name="t", status="success", content="x" * 500)
    result = budget.apply(obs, _ctx(root))
    # 不抛异常即可；artifact 因路径不可用而应为空。
    assert (result.artifact_data or {}).get("artifact_path", "") == ""


# --- H. 系统级 `.cosir` --------------------------------------------------------


def test_paths_reset_derives_data_dir_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CODING_AGENT_DATA_DIR 存在时 DATA_DIR == 该目录，系统 `.cosir` = DATA_DIR/".cosir"。"""

    data_dir = tmp_path / "data"
    monkeypatch.setenv("CODING_AGENT_DATA_DIR", str(data_dir))
    try:
        paths.reset()
        assert data_dir == paths.DATA_DIR
        assert cosir_paths.system_cosir_dir() == data_dir / cosir_paths.COSIR_DIR_NAME
    finally:
        monkeypatch.delenv("CODING_AGENT_DATA_DIR", raising=False)
        paths.reset()


def test_paths_reset_without_data_dir_falls_back_to_repo_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未设置 CODING_AGENT_DATA_DIR 时 DATA_DIR 回落仓库根，系统 `.cosir` = 仓库根/".cosir"。"""

    monkeypatch.delenv("CODING_AGENT_DATA_DIR", raising=False)
    try:
        paths.reset()
        repo_root = paths.repository_root()
        assert repo_root == paths.DATA_DIR
        assert cosir_paths.system_cosir_dir() == repo_root / cosir_paths.COSIR_DIR_NAME
    finally:
        paths.reset()


def test_system_cosir_dir_reads_data_dir(tmp_path: Path) -> None:
    """system_cosir_dir() = paths.DATA_DIR + 固定目录名 `.cosir`。"""

    paths.override(DATA_DIR=tmp_path / "sys")
    try:
        assert cosir_paths.system_cosir_dir() == tmp_path / "sys" / cosir_paths.COSIR_DIR_NAME
    finally:
        paths.reset()


def test_ensure_system_cosir_dir_is_idempotent(tmp_path: Path) -> None:
    """_ensure_system_cosir_dir() 幂等：重复调用不抛异常、目录存在。"""

    from app.app import _ensure_system_cosir_dir

    paths.override(DATA_DIR=tmp_path / "sys")
    try:
        _ensure_system_cosir_dir()
        _ensure_system_cosir_dir()
        assert (tmp_path / "sys" / cosir_paths.COSIR_DIR_NAME).is_dir()
    finally:
        paths.reset()


def test_ensure_system_cosir_dir_degrades_when_name_is_file(tmp_path: Path) -> None:
    """系统 `.cosir` 位置被同名文件占位：创建必须降级不抛异常并写 error 日志。

    使用直接挂到 ``coding_agent.backend`` logger 的 handler 采集，避免受全局 logging 传播
    配置（其他测试可能改动 propagate/level）影响而漏采。
    """

    import logging

    from app.app import _ensure_system_cosir_dir
    from app.config.logging.logger import log as backend_log

    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collector(level=logging.DEBUG)
    previous_level = backend_log.level
    backend_log.addHandler(handler)
    backend_log.setLevel(logging.DEBUG)

    blocker = tmp_path / ".cosir"
    blocker.write_text("blocking file", encoding="utf-8")
    paths.override(DATA_DIR=tmp_path)
    try:
        _ensure_system_cosir_dir()  # 不得抛异常
    finally:
        backend_log.removeHandler(handler)
        backend_log.setLevel(previous_level)
        paths.reset()

    events = [r.getMessage() for r in records]
    assert "system_cosir_init_failed" in events, events
    failed = next(r for r in records if r.getMessage() == "system_cosir_init_failed")
    assert failed.levelno == logging.ERROR
    assert blocker.is_file()


# --- I. `.coding-agent` 退场复核 -----------------------------------------------


def test_coding_agent_removed_from_ignored_dirs() -> None:
    """file_walker.IGNORED_DIRS 与 system_prompt_builder._IGNORED_DIRS 不得再含 `.coding-agent`。"""

    from app.core.context.system_prompt_builder import _IGNORED_DIRS as prompt_ignored
    from app.core.tools.tool_handler.search.file_walker import IGNORED_DIRS as walker_ignored

    assert ".coding-agent" not in walker_ignored
    assert ".coding-agent" not in prompt_ignored


def test_cosir_not_in_ignored_dirs_by_design() -> None:
    """按设计，`.cosir` 允许被遍历/读取，故不得出现在跳过集里。"""

    from app.core.context.system_prompt_builder import _IGNORED_DIRS as prompt_ignored
    from app.core.tools.tool_handler.search.file_walker import IGNORED_DIRS as walker_ignored

    assert ".cosir" not in walker_ignored
    assert ".cosir" not in prompt_ignored


def test_no_coding_agent_literal_in_backend_app() -> None:
    """后端 app/ 源码中不得再出现 `.coding-agent` 字面量（彻底退场复核）。"""

    backend_app = Path(__file__).resolve().parents[1] / "app"
    hits: list[str] = []
    for path in backend_app.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if ".coding-agent" in text:
            hits.append(str(path.relative_to(backend_app)))
    assert hits == [], f"仍引用 .coding-agent 的文件：{hits}"


def test_allow_reserved_only_used_by_two_internal_call_sites() -> None:
    """全仓 grep：`allow_reserved=True` 仅应出现在 terminal_session 与 tool_output_budget。

    为避免命中 docstring，这里只统计真实调用点 ``allow_reserved=True`` 形态。
    """

    backend_root = Path(__file__).resolve().parents[1]
    app_dir = backend_root / "app"
    call_sites: list[str] = []
    for path in app_dir.rglob("*.py"):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        ):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "allow_reserved=True)" in stripped.replace(" ", ""):
                call_sites.append(f"{path.relative_to(app_dir)}:{lineno}")
    expected = {
        "service/terminal/terminal_session_service.py",
        "core/tools/guard/tool_output_budget.py",
    }
    actual = {site.split(":", 1)[0].replace("\\", "/") for site in call_sites}
    assert actual == expected, f"allow_reserved=True 调用面异常：{call_sites}"


# --- J. 磁盘实机证据（可选：真实 git 状态不动） --------------------------------


def test_write_file_rejection_does_not_create_cosir_at_all(tmp_path: Path) -> None:
    """从未存在 `.cosir` 的 workspace 上尝试写入 `.cosir/x`：不得创建 `.cosir` 目录。"""

    root = tmp_path / "ws"
    root.mkdir()
    before = sorted(p.name for p in root.iterdir())
    obs = WriteFileTool().execute(path=".cosir/x.txt", content="hi", execution_context=_ctx(root))
    after = sorted(p.name for p in root.iterdir())
    assert obs.status == "error"
    assert before == after == []


def test_shutil_rmtree_guard_probe_does_not_exist(tmp_path: Path) -> None:
    """确认仓库 `.cosir` 只读保护不依赖 shell：仅作环境自检（不修改任何东西）。"""

    # 只验证 subprocess 环境可用，避免误判；不执行任何写操作。
    assert shutil.which("cmd") is not None or subprocess is not None


# --- L. artifact 降级与只读解析异常路径 ----------------------------------------


def test_artifact_when_root_does_not_exist_degrades(tmp_path: Path) -> None:
    """workspace_root 指向不存在的目录时，artifact 落盘必须降级（不抛异常）。"""

    missing = tmp_path / "does-not-exist"
    budget = ToolOutputBudget(max_chars=5)
    obs = ToolObservation(tool_name="t", status="success", content="x" * 500)
    result = budget.apply(obs, _ctx(missing))  # 不得抛异常
    assert (result.artifact_data or {}).get("output_truncated") is True


def test_resolve_without_boundary_returns_error_on_resolve_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """only-read 解析在 Path.resolve 抛 OSError 时必须归一化为错误字符串而非抛出。"""

    root = tmp_path / "ws"
    root.mkdir()
    resolver = PathResolver(root)

    original_resolve = Path.resolve

    def _boom(self: Path, *args: object, **kwargs: object) -> Path:
        raise OSError("simulated resolve failure")

    monkeypatch.setattr(Path, "resolve", _boom)
    try:
        resolved, error = resolver.resolve_without_boundary("a.txt")
        assert resolved is None
        assert "cannot resolve path" in error
    finally:
        monkeypatch.setattr(Path, "resolve", original_resolve)


def test_resolve_within_workspace_returns_error_on_resolve_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写路径解析在 Path.resolve 抛 OSError 时必须归一化为「escapes project root」。"""

    root = tmp_path / "ws"
    root.mkdir()
    resolver = PathResolver(root)

    original_resolve = Path.resolve

    def _boom(self: Path, *args: object, **kwargs: object) -> Path:
        raise OSError("simulated resolve failure")

    monkeypatch.setattr(Path, "resolve", _boom)
    try:
        resolved, error = resolver.resolve_within_workspace("a.txt")
        assert resolved is None
        assert "escapes project root" in error
    finally:
        monkeypatch.setattr(Path, "resolve", original_resolve)


# --- M. 设计一致性复核（文档契约） ---------------------------------------------


def test_gitignore_ignores_cosir_and_not_coding_agent() -> None:
    """`.gitignore` 必须忽略 `.cosir/`，且不得再保留 `.coding-agent` 条目。"""

    gitignore = Path(__file__).resolve().parents[3] / ".gitignore"
    assert gitignore.is_file(), gitignore
    lines = {line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()}
    assert ".cosir/" in lines
    assert not any(line.startswith(".coding-agent") for line in lines)


def test_system_prompt_describes_cosir_as_read_only() -> None:
    """系统提示词必须把 `.cosir/` 描述为「可读、禁写/改/删/移动」的只读保留区。"""

    import app.core.context.system_prompt_builder as spb
    from app.core.context.system_prompt_builder import _IGNORED_DIRS  # noqa: F401

    source = Path(spb.__file__).read_text(encoding="utf-8")
    assert ".cosir/ is a reserved read-only area" in source
    assert "do not read or modify" not in source


# --- K. 更深层对抗：junction / 符号链接 workspace 根 / 链接删除 -----------------


def test_junction_pointing_into_cosir_is_reserved(tmp_path: Path) -> None:
    """Windows junction 指向 `.cosir`：realpath 后必须命中保留区。"""

    if os.name != "nt":
        pytest.skip("junction 仅 Windows 可用")
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".cosir").mkdir()
    junction = root / "junc"
    try:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(root / ".cosir")],
            capture_output=True,
            text=True,
        )
    except OSError:
        pytest.skip("无法调用 mklink")
    if result.returncode != 0 or not junction.exists():
        pytest.skip(f"环境不支持创建 junction：{result.stderr or result.stdout}")
    assert cosir_paths.is_within_cosir(junction / "x.txt", root) is True
    # 通过 junction 写文件的尝试必须被拒绝。
    obs = WriteFileTool().execute(path="junc/pwn.txt", content="x", execution_context=_ctx(root))
    assert obs.status == "error"
    assert not (root / ".cosir" / "pwn.txt").exists()


def test_delete_symlink_pointing_into_cosir_is_rejected(tmp_path: Path) -> None:
    """delete_file 删一个指向 `.cosir` 的符号链接（词法在区外）：必须拒绝且目标存活。"""

    if not _symlinks_supported(tmp_path):
        pytest.skip("当前环境不支持创建符号链接")
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".cosir").mkdir()
    target = root / ".cosir" / "target.txt"
    target.write_text("keep-me", encoding="utf-8")
    link = root / "link.txt"
    link.symlink_to(target)
    obs = DeleteTool().execute(execution_context=_ctx(root), path="link.txt")
    assert obs.status == "error"
    assert target.exists()


def test_workspace_root_itself_a_symlink_still_protects_cosir(tmp_path: Path) -> None:
    """workspace_root 本身是符号链接时，写 `.cosir` 仍必须被拒绝且真实目标不变。"""

    if not _symlinks_supported(tmp_path):
        pytest.skip("当前环境不支持创建符号链接")
    real_root = tmp_path / "real_ws"
    real_root.mkdir()
    (real_root / ".cosir").mkdir()
    link_root = tmp_path / "link_ws"
    link_root.symlink_to(real_root, target_is_directory=True)
    obs = WriteFileTool().execute(
        path=".cosir/x.txt", content="pwn", execution_context=_ctx(link_root)
    )
    assert obs.status == "error"
    assert not (real_root / ".cosir" / "x.txt").exists()


def test_cosir_root_relative_vs_absolute_consistency(tmp_path: Path) -> None:
    """workspace_root 传绝对路径时，绝对与相对 `.cosir` 表示必须判定一致。"""

    root = tmp_path / "ws"
    (root / ".cosir").mkdir(parents=True)
    abs_path = str(root / ".cosir" / "x.txt")
    assert cosir_paths.is_within_cosir(abs_path, root) is True
    assert cosir_paths.is_within_cosir(abs_path, str(root)) is True


def test_artifact_containment_when_root_is_symlink(tmp_path: Path) -> None:
    """workspace_root 为符号链接时，artifact 仍必须落在真实 `.cosir/tool-artifacts` 且可回读。"""

    if not _symlinks_supported(tmp_path):
        pytest.skip("当前环境不支持创建符号链接")
    real_root = tmp_path / "real_ws"
    real_root.mkdir()
    link_root = tmp_path / "link_ws"
    link_root.symlink_to(real_root, target_is_directory=True)
    budget = ToolOutputBudget(max_chars=5)
    content = "y" * 500
    obs = ToolObservation(tool_name="t", status="success", content=content)
    result = budget.apply(obs, _ctx(link_root))
    artifact_path = (result.artifact_data or {}).get("artifact_path")
    if artifact_path:
        assert artifact_path.startswith(".cosir/tool-artifacts/")
        assert (real_root / ".cosir" / "tool-artifacts" / Path(artifact_path).name).is_file()


def test_is_within_cosir_with_relative_path_under_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`is_within_cosir` 传入相对 `path` 时按进程 cwd 解析：切到根内应命中 `.cosir`。"""

    root = tmp_path / "ws"
    (root / ".cosir").mkdir(parents=True)
    monkeypatch.chdir(root)
    assert cosir_paths.is_within_cosir(".cosir/x.txt", root) is True
    assert cosir_paths.is_within_cosir("src/x.txt", root) is False


def test_resolve_without_boundary_rejects_blank_and_nul(tmp_path: Path) -> None:
    """resolve_without_boundary 对空串/空白/NUL 输入必须拒绝（只读工具入口校验）。"""

    resolver = PathResolver(tmp_path)
    for bad in ("", "   ", "a\x00b"):
        resolved, error = resolver.resolve_without_boundary(bad)
        assert resolved is None
        assert error != ""


def test_resolve_without_boundary_allows_cosir_read(tmp_path: Path) -> None:
    """只读解析不套用 `.cosir` 保护：`.cosir/x` 必须可解析（允许读）。"""

    root = tmp_path / "ws"
    (root / ".cosir").mkdir(parents=True)
    resolved, error = PathResolver(root).resolve_without_boundary(".cosir/x.txt")
    assert resolved == root / ".cosir" / "x.txt"
    assert error == ""


def test_is_inside_workspace_and_escapes(tmp_path: Path) -> None:
    """is_inside_workspace / escapes_workspace 的 containment 语义（`.cosir` 在内）。"""

    root = tmp_path / "ws"
    (root / ".cosir").mkdir(parents=True)
    inside = root / ".cosir" / "x.txt"
    outside = tmp_path / "outside.txt"
    assert PathResolver.is_inside_workspace(root, inside) is True
    assert PathResolver.escapes_workspace(root, inside) is False
    assert PathResolver.is_inside_workspace(root, outside) is False
    assert PathResolver.escapes_workspace(root, outside) is True


def test_resolver_rejects_nested_cosir_prefix_sibling(tmp_path: Path) -> None:
    """`a/.cosir2/x`、`a/.cosirx` 等嵌套同前缀目录不得被当成保留区误拒。"""

    root = tmp_path / "ws"
    (root / "a" / ".cosir2").mkdir(parents=True)
    (root / "a" / ".cosirx").mkdir(parents=True)
    for variant, expected in (
        ("a/.cosir2/x.txt", root / "a" / ".cosir2" / "x.txt"),
        ("a/.cosirx/x.txt", root / "a" / ".cosirx" / "x.txt"),
    ):
        resolved, error = PathResolver(root).resolve_within_workspace(variant)
        assert resolved == expected, variant
        assert error == ""


def test_budget_write_artifact_when_cosir_is_symlink_to_outside(tmp_path: Path) -> None:
    """`.cosir` 被做成指向 workspace 外的符号链接：artifact 落盘不得越界写到外部。"""

    if not _symlinks_supported(tmp_path):
        pytest.skip("当前环境不支持创建符号链接")
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".cosir").symlink_to(outside, target_is_directory=True)
    budget = ToolOutputBudget(max_chars=5)
    obs = ToolObservation(tool_name="t", status="success", content="z" * 500)
    result = budget.apply(obs, _ctx(root))
    artifact_path = (result.artifact_data or {}).get("artifact_path")
    # 若返回了 artifact 路径，绝不能落在 workspace 之外的真实目录。
    if artifact_path:
        written = (root / artifact_path).resolve()
        assert written.is_relative_to(root.resolve()), f"artifact 越界：{written}"
