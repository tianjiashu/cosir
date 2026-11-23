"""受保护路径：文件工具不得读写系统 ``.cosir``（cosir 专属 Skill 目录只读放行）。

数据根按 ``system_cosir.override(DATA_DIR=...)`` 指到临时目录，验证规则随路径变量走、不写死。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.permission.protected_paths import ProtectedPaths
from app.core.tool.schemas import ToolExecutionContext
from app.tools.tool_handler.file.find_files import FindFilesTool
from app.tools.tool_handler.file.read_file import ReadFileTool
from app.tools.tool_handler.file.search_content import SearchContentTool
from app.tools.tool_handler.file.write_file import WriteFileTool
from app.utils.path import system_cosir as paths

MARKER = "sk-protected-marker"


@pytest.fixture
def home(tmp_path: Path) -> Iterator[Path]:
    """数据根：``<home>/.cosir`` 放密钥与 cosir 专属 Skill，``<home>/proj`` 是工作区。"""

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


def test_cosir_is_protected_and_its_skills_are_read_only(home: Path) -> None:
    protected = ProtectedPaths.current()
    skill_file = home / ".cosir" / "skills" / "demo" / "SKILL.md"

    assert protected.denies_read(home / ".cosir" / ".env")
    assert protected.denies_write(home / ".cosir" / ".env")
    assert not protected.denies_read(skill_file)
    assert protected.denies_write(skill_file)
    assert not protected.denies_read(home / "proj" / "notes.txt")
    assert not protected.denies_write(home / ".cosir2" / "x")


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


def _case_insensitive(directory: Path) -> bool:
    return (directory / ".COSIR").exists() == (directory / ".cosir").exists() and (
        directory / ".COSIR"
    ).exists()


def test_case_variant_cannot_bypass_protection(home: Path) -> None:
    """``~/.COSIR/.env`` 在不区分大小写的卷上就是 ``~/.cosir/.env``：读、写、搜索都要拦住。

    曾经按字符串比较（``realpath`` 保留输入大小写、POSIX 上 ``normcase`` 不做事），这种写法被放行。
    """

    if not _case_insensitive(home):
        pytest.skip("文件系统区分大小写，.COSIR 是另一个目录")
    variant = home / ".COSIR"
    ctx = _ctx(home / "proj")

    read = ReadFileTool().execute(path=str(variant / ".env"), execution_context=ctx)
    write = WriteFileTool().execute(
        path=str(variant / "config" / "models.json"), content="{}", execution_context=_ctx(home)
    )
    search = SearchContentTool().execute(pattern=MARKER, path=str(variant), execution_context=ctx)

    assert read.status == "error" and MARKER not in read.content
    assert write.status == "error"
    assert MARKER in (home / ".cosir" / "config" / "models.json").read_text(encoding="utf-8")
    assert search.status == "error" and MARKER not in search.content
    assert ProtectedPaths.current().is_denied_root(variant)


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="firmlink 只在 macOS 上存在")
def test_firmlink_prefix_cannot_bypass_protection(home: Path) -> None:
    """``/System/Volumes/Data/...`` 与 ``/...`` 是同一个目录（firmlink），身份相同，照样拦住。"""

    firmlinked = Path("/System/Volumes/Data") / str(home.resolve()).lstrip("/")
    if not firmlinked.exists():
        pytest.skip("本机没有 Data 卷 firmlink")
    result = ReadFileTool().execute(
        path=str(firmlinked / ".cosir" / ".env"), execution_context=_ctx(home / "proj")
    )

    assert result.status == "error"
    assert MARKER not in result.content
