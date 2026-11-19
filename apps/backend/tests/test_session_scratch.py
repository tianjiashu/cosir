"""会话临时目录：本会话可写、所有会话可读、系统 ``.cosir`` 其余部分照旧禁读禁写；随会话删除回收，启动时对账。

数据根按 ``system_cosir.override(DATA_DIR=...)`` 指到临时目录。
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.context import system_prompt_builder as spb
from app.core.permission.protected_paths import ProtectedPaths
from app.core.tool.schemas import ToolExecutionContext
from app.service.session import session_scratch
from app.tools.tool_handler.read_file import ReadFileTool
from app.tools.tool_handler.security.path_resolver import PathResolver
from app.tools.tool_handler.write_file import WriteFileTool
from app.utils.path import system_cosir as paths

MARKER = "sk-protected-marker"


@pytest.fixture
def home(tmp_path: Path) -> Iterator[Path]:
    """数据根：``<home>/.cosir`` 放密钥，``<home>/proj`` 是工作区；会话 1、2 的临时目录已建好。"""

    root = tmp_path / "home"
    cosir = root / ".cosir"
    cosir.mkdir(parents=True)
    (cosir / ".env").write_text(f"OPENAI_API_KEY={MARKER}\n", encoding="utf-8")
    (root / "proj").mkdir()
    paths.override(DATA_DIR=root)
    try:
        session_scratch.ensure_session_scratch(1)
        session_scratch.ensure_session_scratch(2)
        yield root
    finally:
        paths.reset()


@pytest.fixture
def synchronous_removal(monkeypatch: pytest.MonkeyPatch) -> None:
    """删除本来在后台线程里跑；测试里直接同步执行，断言才有确定结果。"""

    class _Inline:
        def __init__(self, target, args, **_kwargs) -> None:
            self._run = lambda: target(*args)

        def start(self) -> None:
            self._run()

    monkeypatch.setattr(session_scratch.threading, "Thread", _Inline)


def _ctx(workspace: Path, session_id: int = 1) -> ToolExecutionContext:
    return ToolExecutionContext(
        step_id="step-1", session_id=session_id, workspace_id=1, workspace_root=workspace, run_id=1
    )


def test_scratch_dir_is_derived_from_session_id_under_system_cosir(home: Path) -> None:
    assert paths.session_scratch_dir(7) == home / ".cosir" / "scratch" / "7"
    assert _ctx(home / "proj", 7).scratch_dir == home / ".cosir" / "scratch" / "7"


def test_own_scratch_is_writable_others_only_readable_rest_of_cosir_untouched(home: Path) -> None:
    protected = ProtectedPaths.current()
    own = paths.session_scratch_dir(1)
    other = paths.session_scratch_dir(2)

    assert not protected.denies_read(own / "a.txt")
    assert not protected.denies_read(other / "a.txt")
    assert not protected.denies_write(own / "a.txt", own_scratch=own)
    assert protected.denies_write(other / "a.txt", own_scratch=own)
    assert protected.denies_write(own / "a.txt")  # 没给出本会话就一律不放行
    assert protected.denies_read(home / ".cosir" / ".env")
    assert protected.denies_write(home / ".cosir" / ".env", own_scratch=own)


def test_write_tool_writes_own_scratch_and_refuses_another_sessions(home: Path) -> None:
    workspace = home / "proj"
    mine = paths.session_scratch_dir(1) / "nested" / "probe.py"
    theirs = paths.session_scratch_dir(2) / "probe.py"

    ok = WriteFileTool().execute(path=str(mine), content="print(1)\n", execution_context=_ctx(workspace, 1))
    refused = WriteFileTool().execute(path=str(theirs), content="x", execution_context=_ctx(workspace, 1))

    assert ok.status == "success" and mine.read_text(encoding="utf-8") == "print(1)\n"
    assert refused.status == "error" and not theirs.exists()
    # 另一个会话能读到它（父 Agent 读子 Agent 的中间产物、分叉读来源写过的东西）。
    read = ReadFileTool().execute(path=str(mine), execution_context=_ctx(workspace, 2))
    assert read.status == "success" and "print(1)" in read.content


def test_scratch_does_not_open_the_rest_of_cosir_or_the_workspace_rules(home: Path) -> None:
    workspace = home / "proj"
    resolver = PathResolver(workspace, scratch_dir=paths.session_scratch_dir(1))

    escaped, error = resolver.resolve_for_write(str(home / ".cosir" / ".env"))
    assert escaped is None and error
    # 工作区写入照旧走可写根与保留区规则。
    inside, _ = resolver.resolve_for_write("src/a.py")
    reserved, reserved_error = resolver.resolve_for_write(".cosir/x")
    assert inside == (workspace / "src" / "a.py").resolve()
    assert reserved is None and "reserved" in reserved_error


def test_missing_scratch_is_not_writable(home: Path) -> None:
    absent = paths.session_scratch_dir(9)
    resolved, error = PathResolver(home / "proj", scratch_dir=absent).resolve_for_write(str(absent / "a"))
    assert resolved is None and error


def test_deleted_sessions_lose_their_scratch_including_read_only_files(
    home: Path, synchronous_removal: None
) -> None:
    target = paths.session_scratch_dir(1) / "objects" / "pack"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    os.chmod(target, stat.S_IREAD)  # git 对象文件是只读的；Windows 上不去掉只读属性就删不掉

    session_scratch.remove_session_scratch({1, 3})

    assert not paths.session_scratch_dir(1).exists()
    assert paths.session_scratch_dir(2).exists()


def test_startup_reconciliation_removes_only_orphans(home: Path, synchronous_removal: None) -> None:
    root = paths.system_scratch_dir()
    (root / "notes").mkdir()  # 名字不是会话 id 的条目不归它管
    session_scratch.ensure_session_scratch(3)

    session_scratch.reclaim_orphan_scratch(lambda ids: ids & {2})

    assert sorted(entry.name for entry in root.iterdir()) == ["2", "notes"]


def test_prompt_states_scratch_dir_and_the_exception_to_never_modify(home: Path) -> None:
    scratch = paths.session_scratch_dir(1)
    with_scratch = spb.SystemPromptBuilder._build_message_context("/workspace", scratch_dir=scratch)
    without = spb.SystemPromptBuilder._build_message_context("/workspace")

    assert f"- Scratch directory: {scratch}." in with_scratch
    assert "COSIR_SCRATCH_DIR" in with_scratch
    assert "never modify either, except your scratch directory" in with_scratch
    assert "Scratch directory" not in without and "never modify either." in without
