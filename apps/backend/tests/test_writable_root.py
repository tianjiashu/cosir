"""可写根：派生时只能收窄，写类文件工具按它判定，相对路径的坐标仍是工作区根。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.context.system_prompt_builder import SystemPromptBuilder
from app.core.permission.writable_root import WritableRootError, absolute, narrow
from app.core.tool.schemas import ToolExecutionContext
from app.models.session_extra import SessionExtra
from app.models.session_kind import SessionType
from app.tools.tool_handler.security.path_resolver import PathResolver
from app.tools.tool_handler.write_file import WriteFileTool


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    for directory in ("pkg/web", "docs", ".cosir/logs"):
        (tmp_path / directory).mkdir(parents=True)
    return tmp_path


def test_narrow_only_accepts_directories_inside_the_current_root(workspace: Path) -> None:
    assert narrow(workspace, None, "pkg") == "pkg"
    assert narrow(workspace, "pkg", "pkg/web") == "pkg/web"
    assert narrow(workspace, "pkg", "pkg") == "pkg"
    assert narrow(workspace, "pkg", str(workspace / "pkg" / "web")) == "pkg/web"
    # 请求的就是工作区根：没有收窄。
    assert narrow(workspace, None, ".") is None


@pytest.mark.parametrize(
    ("current", "requested", "reason"),
    [
        ("pkg", "docs", "only be narrowed"),
        ("pkg", ".", "only be narrowed"),
        ("pkg", "pkg/../docs", "only be narrowed"),
        (None, "..", "only be narrowed"),
        (None, "missing", "not an existing directory"),
        (None, ".cosir/logs", "protected area"),
    ],
)
def test_narrow_rejects_widening_missing_and_protected(
    workspace: Path, current: str | None, requested: str, reason: str
) -> None:
    with pytest.raises(WritableRootError, match=reason):
        narrow(workspace, current, requested)


def test_narrow_follows_symlinks_out_of_the_root(workspace: Path) -> None:
    (workspace / "pkg" / "link").symlink_to(workspace / "docs")
    with pytest.raises(WritableRootError, match="only be narrowed"):
        narrow(workspace, "pkg", "pkg/link")


def test_write_boundary_is_the_writable_root_but_paths_stay_workspace_relative(workspace: Path) -> None:
    resolver = PathResolver(workspace, absolute(workspace, "pkg"))
    resolved, error = resolver.resolve_for_write("pkg/web/a.ts")
    assert resolved == (workspace / "pkg" / "web" / "a.ts").resolve() and error == ""
    # 同一个相对路径写法在工作区内、可写根外：被拒，并说明范围。
    resolved, error = resolver.resolve_for_write("docs/a.md")
    assert resolved is None and "writable root" in error
    # 相对路径不按可写根解析：``web/a.ts`` 指工作区根下的 web，不是 pkg/web。
    resolved, error = resolver.resolve_for_write("web/a.ts")
    assert resolved is None and "writable root" in error
    # 未收窄的解析器行为不变。
    assert PathResolver(workspace).resolve_for_write("docs/a.md")[1] == ""


def _context(workspace: Path, writable_subpath: str | None) -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id=1, workspace_id=1, workspace_root=workspace, step_id="s", run_id=1,
        tool_call_id="c", writable_subpath=writable_subpath,
    )


def test_context_exposes_the_absolute_writable_root(workspace: Path) -> None:
    assert _context(workspace, None).writable_root == workspace
    assert _context(workspace, "pkg").writable_root == workspace / "pkg"


def test_write_file_is_refused_outside_the_writable_root(workspace: Path) -> None:
    tool = WriteFileTool()
    inside = tool.execute(path="pkg/a.txt", content="x", execution_context=_context(workspace, "pkg"))
    outside = tool.execute(path="docs/a.txt", content="x", execution_context=_context(workspace, "pkg"))
    assert inside.status == "success" and (workspace / "pkg" / "a.txt").read_text() == "x"
    assert outside.status == "error" and not (workspace / "docs" / "a.txt").exists()


def test_user_sessions_cannot_carry_a_writable_root() -> None:
    extra = SessionExtra(writable_root="pkg")
    extra.validate_session_type(SessionType.DELEGATED)
    with pytest.raises(ValueError, match="writable root"):
        extra.validate_session_type(SessionType.USER)


def test_system_prompt_states_the_writable_root_only_when_narrowed(workspace: Path) -> None:
    assert "Writable root" not in SystemPromptBuilder._build_message_context(str(workspace))
    narrowed = SystemPromptBuilder._build_message_context(str(workspace), "pkg")
    assert "- Writable root: pkg (relative to the workspace root)" in narrowed
