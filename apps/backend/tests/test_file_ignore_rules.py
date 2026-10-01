"""``<workspace>/.cosir/.fileignore`` 与根 ``.gitignore`` 合并忽略规则测试。

覆盖：文件缺失时的空文件初始化、既有文件解析（注释 / 空行 / ``*.log`` / ``build/`` /
``!keep`` 否定 /
相对路径）、显式空文件「不忽略任何目录」语义、``iter_files`` 与 ``SearchScope`` 的接入、
文件状态协调器
``prepare`` 采样的一致性，以及读写失败与空 workspace 根时的降级与可排查日志。

``.fileignore`` 与 ``.gitignore`` 均采用标准 gitignore（gitwildmatch）语义，合并后任一命中即跳过。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.core.tools.guard.file_tool_state_coordinator import FileToolStateCoordinator
from app.core.tools.schemas import ToolExecutionContext
from app.core.tools.tool_handler.find_files import FindFilesTool
from app.core.tools.tool_handler.search.file_walker import iter_files
from app.core.tools.tool_handler.search.gitignore_rules import (
    GitignoreMatcher,
    build_gitwildmatch_spec,
    load_gitignore_rules,
)
from app.core.tools.tool_handler.search.ignore_rules import (
    DEFAULT_IGNORED_DIR_NAMES,
    IGNORE_FILE_NAME,
    CompositeIgnoreMatcher,
    default_ignore_rules,
    ignore_file_path,
    load_fileignore_rules,
    load_search_ignore_rules,
)
from app.core.tools.tool_handler.search.scope import SearchScope


def _write_ignore_file(workspace: Path, content: str) -> Path:
    """在工作区 ``.cosir`` 下写入指定内容的 ``.fileignore`` 并返回其路径。"""

    path = ignore_file_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _write_gitignore(workspace: Path, content: str) -> Path:
    """在工作区根目录写入指定内容的 ``.gitignore`` 并返回其路径。"""

    path = workspace / ".gitignore"
    path.write_text(content, encoding="utf-8")
    return path


def _touch(path: Path) -> Path:
    """创建文件及其父目录并写入占位内容。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n", encoding="utf-8")
    return path


def _log_events(caplog) -> list[str]:
    """返回捕获到的日志事件名列表（``log.warning(event, ...)`` 的 event 即 message）。"""

    return [record.getMessage() for record in caplog.records]


def test_ignore_file_is_created_empty_on_first_use(tmp_path: Path) -> None:
    """规则文件不存在时，首次加载要创建空文件并返回空匹配器。"""

    rules = load_fileignore_rules(tmp_path)

    created = ignore_file_path(tmp_path)
    assert created == tmp_path / ".cosir" / IGNORE_FILE_NAME
    assert created.read_text(encoding="utf-8") == ""
    assert rules.match_dir(tmp_path / "node_modules") is False
    assert rules.match_dir(tmp_path / "src") is False


def test_default_rules_keep_the_expected_baseline_directories() -> None:
    """默认集合必须保留各生态的基线忽略项（防止默认值被误删导致遍历退化）。"""

    assert {".git", "node_modules", "__pycache__", ".venv", "site-packages"} <= set(
        DEFAULT_IGNORED_DIR_NAMES
    )


def test_existing_ignore_file_is_parsed_as_gitignore(tmp_path: Path) -> None:
    """已有 ``.fileignore`` 以标准 gitignore 语义解析（``build/``、``*.log``、``!keep`` 否定）。"""

    _write_ignore_file(
        tmp_path,
        "\n".join(
            [
                "# 注释行",
                "  build/  ",
                "*.log",
                "!keep.log",
                "",
            ]
        ),
    )

    rules = load_fileignore_rules(tmp_path)

    assert rules.match_dir(tmp_path / "build") is True
    assert rules.match_dir(tmp_path / "src") is False
    assert rules.match_file(tmp_path / "a.log") is True
    assert rules.match_file(tmp_path / "keep.log") is False  # 否定生效
    assert rules.match_file(tmp_path / "keep.txt") is False


def test_empty_existing_ignore_file_means_no_ignore(tmp_path: Path) -> None:
    """文件已存在但只有注释时，语义为「不忽略任何目录」（尊重用户显式配置）。"""

    _write_ignore_file(tmp_path, "# 只有注释\n\n")

    rules = load_fileignore_rules(tmp_path)

    assert rules.match_dir(tmp_path / "node_modules") is False
    assert rules.match_file(tmp_path / "a.log") is False


def test_loader_truncates_rules_beyond_limit_and_logs(tmp_path: Path, caplog) -> None:
    """规则行数超过上限时截断，并写带文件路径的 WARNING 日志（防止异常文件撑大规则集合）。"""

    caplog.set_level(logging.WARNING)
    _write_ignore_file(tmp_path, "\n".join(f"dir_{index}" for index in range(2050)))

    rules = load_fileignore_rules(tmp_path)

    # 前 2000 条生效，超出部分被忽略。
    assert rules.match_dir(tmp_path / "dir_5") is True
    assert rules.match_dir(tmp_path / "dir_2045") is False
    assert "file_ignore_rules_truncated" in _log_events(caplog)


def test_blank_workspace_root_falls_back_to_default_rules(caplog) -> None:
    """workspace 根为空时直接返回默认规则，不在进程当前目录创建规则文件。"""

    caplog.set_level(logging.WARNING)

    rules = load_fileignore_rules("   ")

    assert rules == default_ignore_rules()
    assert "file_ignore_workspace_missing" in _log_events(caplog)


def test_default_rules_ignore_builtin_directories() -> None:
    """内置默认规则按 gitignore 语义跳过基线目录，且不含 ``.cosir`` 与 ``.coding-agent``。"""

    rules = default_ignore_rules()

    assert rules.match_dir(Path("/any/where/node_modules")) is True
    assert rules.match_dir(Path("/any/where/src")) is False
    assert ".cosir" not in DEFAULT_IGNORED_DIR_NAMES
    assert ".coding-agent" not in DEFAULT_IGNORED_DIR_NAMES


def test_walker_skips_configured_directory_subtree(tmp_path: Path) -> None:
    """注入的规则命中目录时，整棵子树都不产出文件。"""

    kept = _touch(tmp_path / "src" / "main.py")
    _touch(tmp_path / "skipme" / "inner" / "nested.py")
    rules = GitignoreMatcher(workspace_root=None, spec=build_gitwildmatch_spec(["skipme"]))

    assert list(iter_files(tmp_path, rules=rules)) == [kept]


def test_walker_skips_relative_path_rules(tmp_path: Path) -> None:
    """相对路径规则（以 workspace 根为基准）只跳过该路径下的目录，同名前缀目录不受影响。"""

    _touch(tmp_path / "a" / "b" / "ignored.py")
    kept = _touch(tmp_path / "a" / "c" / "kept.py")
    rules = GitignoreMatcher(workspace_root=tmp_path, spec=build_gitwildmatch_spec(["a/b/"]))

    assert list(iter_files(tmp_path, rules=rules)) == [kept]


def test_walker_falls_back_to_default_rules_without_injection(tmp_path: Path) -> None:
    """未注入规则时使用内置默认规则（``node_modules`` 等被跳过）。"""

    kept = _touch(tmp_path / "src" / "main.py")
    _touch(tmp_path / "node_modules" / "pkg" / "index.js")

    assert list(iter_files(tmp_path)) == [kept]


def test_search_scope_applies_workspace_fileignore(tmp_path: Path) -> None:
    """``SearchScope.iter_files`` 读取 workspace 的 ``.fileignore`` 决定跳过哪些目录。"""

    _write_ignore_file(tmp_path, "generated\n")
    kept = _touch(tmp_path / "src" / "main.py")
    _touch(tmp_path / "generated" / "gen.py")
    scope = SearchScope.from_path(tmp_path, tmp_path)

    produced = list(scope.iter_files())

    assert kept in produced
    assert tmp_path / "generated" / "gen.py" not in produced
    # .cosir 按设计允许遍历，规则文件自身可见。
    assert ignore_file_path(tmp_path) in produced


def test_prepare_observed_paths_follows_workspace_fileignore(tmp_path: Path) -> None:
    """文件状态协调器经公开 ``prepare`` 采样时遵循同一忽略规则，避免把被忽略目录计入。"""

    _write_ignore_file(tmp_path, "generated\n")
    kept = _touch(tmp_path / "src" / "main.py")
    _touch(tmp_path / "generated" / "gen.py")
    definition = FindFilesTool().to_definition()
    context = ToolExecutionContext(
        task_id=1,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=0,
    )

    plan = FileToolStateCoordinator().prepare(
        definition,
        {"path": ".", "pattern": "*.py"},
        context,
        tool_call_id="call-ignore-rules",
    )

    assert plan.snapshot_complete is True
    assert kept in plan.observed_paths
    assert tmp_path / "generated" / "gen.py" not in plan.observed_paths


def test_read_failure_degrades_to_default_rules_and_logs(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """规则文件读取失败时降级为默认规则并写 WARNING 日志，不阻断调用方。"""

    caplog.set_level(logging.WARNING)
    original_open = Path.open

    def _open(self: Path, *args: object, **kwargs: object):
        mode = args[0] if args else kwargs.get("mode", "r")
        if mode == "r":
            raise OSError("read denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _open)

    rules = load_fileignore_rules(tmp_path)

    assert rules.match_dir(tmp_path / "node_modules") is True
    assert "file_ignore_read_failed" in _log_events(caplog)


def test_create_failure_degrades_to_default_rules_and_logs(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """规则文件创建失败时降级为默认规则并写 WARNING 日志，不阻断搜索。"""

    caplog.set_level(logging.WARNING)
    original_open = Path.open

    def _open(self: Path, *args: object, **kwargs: object):
        mode = args[0] if args else kwargs.get("mode", "r")
        if mode == "x":
            raise OSError("create denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _open)

    rules = load_fileignore_rules(tmp_path)

    assert rules.match_dir(tmp_path / "node_modules") is True
    assert not ignore_file_path(tmp_path).exists()
    assert "file_ignore_create_failed" in _log_events(caplog)


def test_gitignore_skips_files_and_directories(tmp_path: Path) -> None:
    """根 ``.gitignore`` 同时覆盖文件级（``*.log``）与目录级（``build/``）忽略。"""

    _write_gitignore(tmp_path, "*.log\nbuild/\n")
    kept = _touch(tmp_path / "src" / "main.py")
    _touch(tmp_path / "build" / "gen.py")  # 整棵子树跳过
    _touch(tmp_path / "data" / "dump.log")  # 文件级跳过

    produced = list(SearchScope.from_path(tmp_path, tmp_path).iter_files())

    assert kept in produced
    assert tmp_path / "build" / "gen.py" not in produced
    assert tmp_path / "data" / "dump.log" not in produced


def test_search_scope_combines_fileignore_and_gitignore(tmp_path: Path) -> None:
    """``SearchScope`` 同时尊重两个 gitignore 语法的规则文件。"""

    _write_ignore_file(tmp_path, "generated\n")
    _write_gitignore(tmp_path, "*.log\n")
    kept = _touch(tmp_path / "src" / "main.py")
    _touch(tmp_path / "generated" / "gen.py")  # .fileignore
    _touch(tmp_path / "data" / "dump.log")  # .gitignore

    produced = list(SearchScope.from_path(tmp_path, tmp_path).iter_files())

    assert kept in produced
    assert tmp_path / "generated" / "gen.py" not in produced
    assert tmp_path / "data" / "dump.log" not in produced


def test_load_gitignore_rules_absent_returns_none(tmp_path: Path) -> None:
    """根 ``.gitignore`` 缺失时 ``load_gitignore_rules`` 返回 None（不参与忽略）。"""

    assert load_gitignore_rules(tmp_path) is None


def test_load_gitignore_rules_parses_patterns(tmp_path: Path) -> None:
    """``.gitignore`` 解析为匹配器，文件级/否定/目录级均按 git 语义生效。"""

    _write_gitignore(tmp_path, "*.log\nbuild/\n!keep.log\n")
    matcher = load_gitignore_rules(tmp_path)

    assert isinstance(matcher, GitignoreMatcher)
    assert matcher is not None
    assert matcher.match_file(tmp_path / "a.log") is True
    assert matcher.match_file(tmp_path / "keep.log") is False
    assert matcher.match_dir(tmp_path / "build") is True


def test_load_search_ignore_rules_returns_composite_when_gitignore_present(tmp_path: Path) -> None:
    """同时存在 ``.gitignore`` 时组合加载器返回 ``CompositeIgnoreMatcher`` 并合并两类来源。"""

    _write_gitignore(tmp_path, "*.log\n")
    matcher = load_search_ignore_rules(tmp_path)

    assert isinstance(matcher, CompositeIgnoreMatcher)
    assert matcher.match_file(tmp_path / "a.log") is True
    assert matcher.match_file(tmp_path / "a.py") is False


def test_load_search_ignore_rules_falls_back_to_fileignore_alone(tmp_path: Path) -> None:
    """仅 ``.fileignore`` 时直接返回其规则对象（不包装），且仍满足 IgnoreMatcher 协议。"""

    _write_ignore_file(tmp_path, "generated\n")
    matcher = load_search_ignore_rules(tmp_path)

    assert not isinstance(matcher, CompositeIgnoreMatcher)
    # ``generated``（无斜杠）按 gitignore 语义忽略任意层级名为 generated 的目录及其子树。
    assert matcher.match_dir(tmp_path / "generated") is True
    assert matcher.match_file(tmp_path / "generated" / "x.py") is True
