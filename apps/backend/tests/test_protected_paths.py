"""受保护路径：文件工具不得读写系统 ``.cosir``（Skill 目录只读放行）。

数据根按 ``system_cosir.override(DATA_DIR=...)`` 指到临时目录，验证规则随路径变量走、不写死。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.permission.protected_paths import ProtectedPaths
from app.core.tool.schemas import ToolExecutionContext
from app.tools.tool_handler.find_files import FindFilesTool
from app.tools.tool_handler.read_file import ReadFileTool
from app.tools.tool_handler.search_content import SearchContentTool
from app.tools.tool_handler.write_file import WriteFileTool
from app.utils.path import system_cosir as paths

MARKER = "sk-protected-marker"


@pytest.fixture
def home(tmp_path: Path) -> Iterator[Path]:
    """数据根：``<home>/.cosir`` 放密钥与 Skill，``<home>/proj`` 是工作区。"""

    root = tmp_path / "home"
    cosir = root / ".cosir"
    (cosir / "config").mkdir(parents=True)
    (cosir / ".env").write_text(f"OPENAI_API_KEY={MARKER}\n", encoding="utf-8")
    (cosir / "config" / "models.json").write_text(f'{{"key": "{MARKER}"}}', encoding="utf-8")
    skill = cosir / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Demo skill\n", encoding="utf-8")
    project = root / "proj"
    project.mkdir()
    (project / "notes.txt").write_text(f"public {MARKER}\n", encoding="utf-8")
    paths.override(DATA_DIR=root)
    try:
        yield root
    finally:
        paths.reset()


def _ctx(workspace: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        step_id="step-1", session_id=1, workspace_id=1, workspace_root=workspace, run_id=1
    )


def test_most_specific_rule_wins(home: Path) -> None:
    protected = ProtectedPaths.current()

    assert protected.access(home / ".cosir" / ".env") == "deny"
    assert protected.access(home / ".cosir" / "skills" / "demo" / "SKILL.md") == "read"
    assert protected.access(home / "proj" / "notes.txt") is None
    assert protected.access(home / ".cosir2" / "x") is None


def test_read_file_denies_system_env_but_reads_skills(home: Path) -> None:
    ctx = _ctx(home / "proj")

    denied = ReadFileTool().execute(path=str(home / ".cosir" / ".env"), execution_context=ctx)
    allowed = ReadFileTool().execute(
        path=str(home / ".cosir" / "skills" / "demo" / "SKILL.md"), execution_context=ctx
    )

    assert denied.status == "error"
    assert MARKER not in denied.content
    assert "cosir's own data directory" in denied.content
    assert allowed.status == "success"
    assert "Demo skill" in allowed.content


def test_read_file_denies_symlink_into_system_cosir(home: Path) -> None:
    link = home / "proj" / "env-link"
    os.symlink(home / ".cosir" / ".env", link)

    result = ReadFileTool().execute(path="env-link", execution_context=_ctx(home / "proj"))

    assert result.status == "error"
    assert MARKER not in result.content


def test_search_content_skips_system_cosir_when_walking_from_above(home: Path) -> None:
    result = SearchContentTool().execute(
        pattern=MARKER, path=str(home), execution_context=_ctx(home / "proj")
    )

    assert result.status == "success"
    assert "notes.txt" in result.content
    assert ".env" not in result.content
    assert "models.json" not in result.content


def test_search_content_rejects_system_cosir_as_scope(home: Path) -> None:
    result = SearchContentTool().execute(
        pattern=MARKER, path=str(home / ".cosir"), execution_context=_ctx(home / "proj")
    )

    assert result.status == "error"
    assert MARKER not in result.content


def test_find_files_skips_system_cosir(home: Path) -> None:
    result = FindFilesTool().execute(
        pattern="*.json", path=str(home), execution_context=_ctx(home / "proj")
    )

    assert "models.json" not in result.content


def test_write_file_denies_system_cosir_inside_workspace(home: Path) -> None:
    target = home / ".cosir" / "config" / "models.json"

    result = WriteFileTool().execute(path=str(target), content="{}", execution_context=_ctx(home))

    assert result.status == "error"
    assert MARKER in target.read_text(encoding="utf-8")


def test_rules_follow_data_dir_override(home: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    (other / ".cosir").mkdir(parents=True)
    paths.override(DATA_DIR=other)

    result = ReadFileTool().execute(
        path=str(home / ".cosir" / ".env"), execution_context=_ctx(home / "proj")
    )

    assert result.status == "success"
    assert ProtectedPaths.current().denies_read(other / ".cosir" / "x")
