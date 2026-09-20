"""workspace ``.cosir`` 只读保留区保护的行为测试。

覆盖四层：

1. ``PathResolver.resolve_within_workspace`` 的保留区判定（含 ``.cosir`` 目录本身、
   ``allow_reserved`` 显式放行、同前缀兄弟目录不误判）；
2. 五个文件工具（write_file / patch_write / apply_patch / delete_file / move_file）端到端被拒绝，
   且**磁盘状态不变**；
3. 只读工具仍可读取 ``.cosir``（产品要求「允许读」）；
4. 调度期资源路径解析（路径锁 / revision 登记入口）与终端 cwd 的豁免边界。
"""

from pathlib import Path

import pytest

from app.core.tools.guard.file_resource_paths import (
    FileResourcePathError,
    FileResourceResolver,
)
from app.core.tools.schemas import ToolExecutionContext
from app.core.tools.tool_handler.apply_patch_tool import ApplyPatchTool
from app.core.tools.tool_handler.delete_tool import DeleteTool
from app.core.tools.tool_handler.move_tool import MoveTool
from app.core.tools.tool_handler.read_file import ReadFileTool
from app.core.tools.tool_handler.replace_tool import ReplaceTool
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.core.tools.tool_handler.write_file import WriteFileTool
from app.service.terminal.terminal_session_service import TerminalSessionService


def _context(root: Path) -> ToolExecutionContext:
    """构造绑定到给定 workspace 根的执行上下文。"""

    return ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=root, run_id=1)


# --- 1. PathResolver 单元行为 -------------------------------------------------


def test_write_path_inside_cosir_is_rejected(tmp_path: Path) -> None:
    """落在 ``.cosir`` 下的写路径必须被拒绝，错误信息点名保留区。"""

    root = tmp_path / "ws"
    root.mkdir()
    resolved, error = PathResolver(root).resolve_within_workspace(".cosir/Attachment/a.png")
    assert resolved is None
    assert ".cosir" in error


def test_cosir_directory_itself_is_rejected(tmp_path: Path) -> None:
    """``.cosir`` 目录本身也必须被拒绝，不能被当作可写目录。"""

    root = tmp_path / "ws"
    root.mkdir()
    resolved, error = PathResolver(root).resolve_within_workspace(".cosir")
    assert resolved is None
    assert error


def test_allow_reserved_lets_internal_writers_through(tmp_path: Path) -> None:
    """显式 ``allow_reserved=True`` 时保留区放行（内部子系统使用）。"""

    root = tmp_path / "ws"
    root.mkdir()
    resolved, error = PathResolver(root).resolve_within_workspace(
        ".cosir/tool-artifacts/x.txt",
        allow_reserved=True,
    )
    assert resolved == root / ".cosir" / "tool-artifacts" / "x.txt"
    assert error == ""


def test_path_outside_cosir_still_resolves(tmp_path: Path) -> None:
    """保留区之外的路径行为不变。"""

    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    resolved, error = PathResolver(root).resolve_within_workspace("src/a.py")
    assert resolved == root / "src" / "a.py"
    assert error == ""


def test_cosir_prefixed_sibling_directory_is_not_reserved(tmp_path: Path) -> None:
    """``.cosir2`` 这类同前缀兄弟目录不得被误判为保留区。"""

    root = tmp_path / "ws"
    (root / ".cosir2").mkdir(parents=True)
    resolved, error = PathResolver(root).resolve_within_workspace(".cosir2/a.txt")
    assert resolved == root / ".cosir2" / "a.txt"
    assert error == ""


# --- 2. 五个文件工具端到端（磁盘必须不变） ------------------------------------


def test_write_file_cannot_write_into_cosir(tmp_path: Path) -> None:
    """write_file 指向 ``.cosir`` 时返回 error，且磁盘不产生文件。"""

    (tmp_path / ".cosir").mkdir()
    observation = WriteFileTool().execute(
        path=".cosir/x.txt",
        content="hi",
        execution_context=_context(tmp_path),
    )
    assert observation.status == "error"
    assert not (tmp_path / ".cosir" / "x.txt").exists()


def test_patch_write_cannot_edit_inside_cosir(tmp_path: Path) -> None:
    """patch_write 指向 ``.cosir`` 时返回 error，原文件内容不变。"""

    target = tmp_path / ".cosir" / "a.txt"
    target.parent.mkdir()
    target.write_text("hello", encoding="utf-8")
    observation = ReplaceTool().execute(
        execution_context=_context(tmp_path),
        path=".cosir/a.txt",
        old_string="hello",
        new_string="bye",
    )
    assert observation.status == "error"
    assert target.read_text(encoding="utf-8") == "hello"


def test_apply_patch_cannot_patch_inside_cosir(tmp_path: Path) -> None:
    """apply_patch 指向 ``.cosir`` 时返回 error，原文件内容不变。"""

    target = tmp_path / ".cosir" / "a.txt"
    target.parent.mkdir()
    target.write_text("hello\n", encoding="utf-8")
    patch = (
        "diff --git a/.cosir/a.txt b/.cosir/a.txt\n"
        "--- a/.cosir/a.txt\n"
        "+++ b/.cosir/a.txt\n"
        "@@ -1 +1 @@\n"
        "-hello\n"
        "+bye\n"
    )
    observation = ApplyPatchTool().execute(execution_context=_context(tmp_path), patch=patch)
    assert observation.status == "error"
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_delete_file_cannot_delete_inside_cosir(tmp_path: Path) -> None:
    """delete_file 指向 ``.cosir`` 时返回 error，文件必须存活。"""

    target = tmp_path / ".cosir" / "Attachment" / "a.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    observation = DeleteTool().execute(
        execution_context=_context(tmp_path),
        path=".cosir/Attachment/a.png",
    )
    assert observation.status == "error"
    assert target.exists()


def test_move_file_cannot_move_into_cosir(tmp_path: Path) -> None:
    """move_file 目标落在 ``.cosir`` 时返回 error，源文件必须存活。"""

    source = tmp_path / "src.txt"
    source.write_text("a", encoding="utf-8")
    (tmp_path / ".cosir").mkdir()
    observation = MoveTool().execute(
        execution_context=_context(tmp_path),
        source_path="src.txt",
        destination_path=".cosir/src.txt",
    )
    assert observation.status == "error"
    assert source.exists()
    assert not (tmp_path / ".cosir" / "src.txt").exists()


def test_move_file_cannot_move_out_of_cosir(tmp_path: Path) -> None:
    """move_file 源落在 ``.cosir`` 时同样被拒绝，文件必须存活。"""

    source = tmp_path / ".cosir" / "src.txt"
    source.parent.mkdir()
    source.write_text("a", encoding="utf-8")
    observation = MoveTool().execute(
        execution_context=_context(tmp_path),
        source_path=".cosir/src.txt",
        destination_path="src.txt",
    )
    assert observation.status == "error"
    assert source.exists()


# --- 3. 只读工具仍可读 ---------------------------------------------------------


def test_read_file_can_still_read_cosir(tmp_path: Path) -> None:
    """只读工具不受保留区限制（产品要求「允许读」）。"""

    target = tmp_path / ".cosir" / "a.txt"
    target.parent.mkdir()
    target.write_text("hello", encoding="utf-8")
    observation = ReadFileTool().execute(
        path=".cosir/a.txt",
        execution_context=_context(tmp_path),
    )
    assert observation.status == "success"


# --- 4. 调度期资源路径与终端边界 ----------------------------------------------


def test_resource_resolution_rejects_cosir_write_path(tmp_path: Path) -> None:
    """调度期写路径解析同样拒绝 ``.cosir``（不进入路径锁 / revision 登记）。"""

    root = tmp_path / "ws"
    root.mkdir()
    with pytest.raises(FileResourcePathError):
        FileResourceResolver(root).resolve("write_file", {"path": ".cosir/x.txt"})


def test_terminal_cwd_inside_cosir_is_allowed(tmp_path: Path) -> None:
    """终端 cwd 是保留区的唯一豁免点之一，落在 ``.cosir`` 不得抛错。"""

    root = tmp_path / "ws"
    cosir = root / ".cosir"
    cosir.mkdir(parents=True)
    assert TerminalSessionService._resolve_cwd(str(root), ".cosir") == cosir
