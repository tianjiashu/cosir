"""``PathResolver`` 对开头 ``~``（用户主目录）的展开。

``Path`` 本身不展开 ``~``：修复前 ``~/x`` 被当作工作区下名为 ``~`` 的目录，读报不存在、写入落进工作区。
"""

from pathlib import Path

import pytest

from app.tools.file_system.path_resolver import PathResolver


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    workspace = home / "ws"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home, workspace


def test_read_expands_home(layout: tuple[Path, Path]) -> None:
    home, workspace = layout
    (home / ".agents").mkdir()
    (home / ".agents" / "SKILL.md").write_text("x")

    resolved, error = PathResolver(workspace).resolve_for_read("~/.agents/SKILL.md")

    assert error == ""
    assert resolved == (home / ".agents" / "SKILL.md").resolve()


def test_write_outside_workspace_via_home_is_rejected(layout: tuple[Path, Path]) -> None:
    _, workspace = layout

    resolved, error = PathResolver(workspace).resolve_for_write("~/foo.txt")

    assert resolved is None
    assert "escapes project root" in error
    assert not (workspace / "~").exists()


def test_write_inside_workspace_via_home_resolves(layout: tuple[Path, Path]) -> None:
    _, workspace = layout

    resolved, error = PathResolver(workspace).resolve_for_write("~/ws/src/a.py")

    assert error == ""
    assert resolved == (workspace / "src" / "a.py").resolve()


@pytest.mark.parametrize("path", ["~user/a.txt", "~a.txt", "src/~/a.txt"])
def test_other_tilde_forms_stay_relative(layout: tuple[Path, Path], path: str) -> None:
    _, workspace = layout

    resolved, error = PathResolver(workspace).resolve_for_read(path)

    assert error == ""
    assert resolved == (workspace / path).resolve()
