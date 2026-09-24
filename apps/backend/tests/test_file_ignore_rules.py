"""``<workspace>/.cosir/.fileignore`` 规则加载、解析与遍历接入测试。

覆盖：文件缺失时的默认初始化、既有文件解析（注释 / 空行 / 尾斜杠 / 相对路径）、空文件的
「不忽略任何目录」语义、目录名与相对路径两类匹配、``iter_files`` 与 ``SearchScope`` 的接入、
文件状态协调器 ``prepare`` 采样的一致性，以及读写失败与空 workspace 根时的降级与可排查日志。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.core.tools.guard.file_tool_state_coordinator import FileToolStateCoordinator
from app.core.tools.schemas import ToolExecutionContext
from app.core.tools.tool_handler.find_files import FindFilesTool
from app.core.tools.tool_handler.search.file_walker import iter_files
from app.core.tools.tool_handler.search.ignore_rules import (
    DEFAULT_IGNORED_DIR_NAMES,
    IGNORE_FILE_NAME,
    IgnoreRules,
    default_ignore_rules,
    ignore_file_path,
    load_ignore_rules,
    parse_ignore_rules,
    render_default_content,
)
from app.core.tools.tool_handler.search.scope import SearchScope


def _write_ignore_file(workspace: Path, content: str) -> Path:
    """在工作区 ``.cosir`` 下写入指定内容的 ``.fileignore`` 并返回其路径。

    参数:
        workspace: 工作区根目录。
        content: 规则文件全文。

    返回:
        规则文件路径。

    异常:
        无（写入失败由 OS 异常向上暴露，属测试装配错误）。

    副作用:
        创建 ``<workspace>/.cosir`` 目录并写入文件。
    """

    path = ignore_file_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _touch(path: Path) -> Path:
    """创建文件及其父目录并写入占位内容。

    参数:
        path: 目标文件路径。

    返回:
        同一路径，便于链式断言。

    异常:
        无（OS 异常向上暴露，属测试装配错误）。

    副作用:
        创建父目录与文件。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n", encoding="utf-8")
    return path


def _log_events(caplog) -> list[str]:
    """返回捕获到的日志事件名列表（``log.warning(event, ...)`` 的 event 即 message）。

    参数:
        caplog: pytest 日志捕获 fixture。

    返回:
        按记录顺序排列的事件名列表。

    异常:
        无。

    副作用:
        无（只读捕获结果）。
    """

    return [record.getMessage() for record in caplog.records]


def test_ignore_file_is_created_with_default_rules_on_first_use(tmp_path: Path) -> None:
    """规则文件不存在时，首次加载要创建它并写入默认规则，返回默认集合。"""

    rules = load_ignore_rules(tmp_path)

    created = ignore_file_path(tmp_path)
    assert created == tmp_path / ".cosir" / IGNORE_FILE_NAME
    assert created.read_text(encoding="utf-8") == render_default_content()
    assert rules.names == frozenset(DEFAULT_IGNORED_DIR_NAMES)
    assert rules.paths == frozenset()
    assert rules.workspace_root == tmp_path


def test_default_rules_keep_the_expected_baseline_directories() -> None:
    """默认集合必须保留各生态的基线忽略项（防止默认值被误删导致遍历退化）。"""

    assert {".git", "node_modules", "__pycache__", ".venv", "site-packages"} <= set(
        DEFAULT_IGNORED_DIR_NAMES
    )


def test_existing_ignore_file_is_the_single_source_of_truth(tmp_path: Path) -> None:
    """文件已存在时以文件内容为准（不再写入默认规则），并区分目录名与相对路径规则。"""

    _write_ignore_file(
        tmp_path,
        "\n".join(
            [
                "# 注释行",
                "  node_modules  ",
                "build/",
                "/sub/dir",
                "",
                "sub\\dir2",
            ]
        ),
    )

    rules = load_ignore_rules(tmp_path)

    assert rules.names == frozenset({"node_modules", "build"})
    assert rules.paths == frozenset({"sub/dir", "sub/dir2"})


def test_empty_ignore_file_means_no_directory_is_ignored(tmp_path: Path) -> None:
    """文件存在但没有任何有效规则时，语义为「不忽略任何目录」。"""

    _write_ignore_file(tmp_path, "# 只有注释\n\n")

    rules = load_ignore_rules(tmp_path)

    assert rules.names == frozenset()
    assert rules.paths == frozenset()


def test_loader_truncates_rules_beyond_limit_and_logs(tmp_path: Path, caplog) -> None:
    """规则行数超过上限时截断，并写带文件路径的 WARNING 日志（防止异常文件撑大规则集合）。"""

    caplog.set_level(logging.WARNING)
    _write_ignore_file(tmp_path, "\n".join(f"dir_{index}" for index in range(2050)))

    rules = load_ignore_rules(tmp_path)

    assert len(rules.names) == 2000
    assert "file_ignore_rules_truncated" in _log_events(caplog)


def test_blank_workspace_root_falls_back_to_default_rules(caplog) -> None:
    """workspace 根为空时直接返回默认规则，不在进程当前目录创建规则文件。"""

    caplog.set_level(logging.WARNING)

    rules = load_ignore_rules("   ")

    assert rules == default_ignore_rules()
    assert "file_ignore_workspace_missing" in _log_events(caplog)


def test_ignores_dir_matches_name_at_any_depth(tmp_path: Path) -> None:
    """不含 "/" 的规则按目录名匹配任意层级。"""

    rules = IgnoreRules(names=frozenset({"skipme"}))

    assert rules.ignores_dir(tmp_path / "skipme") is True
    assert rules.ignores_dir(tmp_path / "a" / "b" / "skipme") is True
    assert rules.ignores_dir(tmp_path / "skipme2") is False


def test_ignores_dir_matches_relative_path_only_below_workspace(tmp_path: Path) -> None:
    """含 "/" 的规则只匹配 workspace 之下该相对路径，越界根退化为只按目录名判定。"""

    rules = IgnoreRules(paths=frozenset({"a/b"}), workspace_root=tmp_path)

    assert rules.ignores_dir(tmp_path / "a" / "b") is True
    assert rules.ignores_dir(tmp_path / "x" / "a" / "b") is False
    assert rules.ignores_dir(tmp_path / "a") is False
    assert rules.ignores_dir(Path("C:/outside/a/b")) is False


def test_default_rules_ignore_no_relative_paths() -> None:
    """内置默认规则只有目录名，且不含 ``.cosir`` 与 ``.coding-agent``。"""

    rules = default_ignore_rules()

    assert rules.names == frozenset(DEFAULT_IGNORED_DIR_NAMES)
    assert rules.paths == frozenset()
    assert rules.workspace_root is None
    assert ".cosir" not in rules.names
    assert ".coding-agent" not in rules.names


def test_parse_rules_is_pure_and_normalizes_separators() -> None:
    """解析函数是纯函数：不访问文件系统、不截断，并按 "/" 归一规则。"""

    rules = parse_ignore_rules("# 注释\nnode_modules\n\nsub\\dir\n", workspace_root=Path("C:/ws"))

    assert rules.names == frozenset({"node_modules"})
    assert rules.paths == frozenset({"sub/dir"})
    assert rules.workspace_root == Path("C:/ws")


def test_walker_skips_configured_directory_subtree(tmp_path: Path) -> None:
    """注入的规则命中目录时，整棵子树都不产出文件。"""

    kept = _touch(tmp_path / "src" / "main.py")
    _touch(tmp_path / "skipme" / "inner" / "nested.py")
    rules = IgnoreRules(names=frozenset({"skipme"}))

    assert list(iter_files(tmp_path, rules=rules)) == [kept]


def test_walker_skips_relative_path_rules(tmp_path: Path) -> None:
    """相对路径规则只跳过该路径下的目录，同名前缀目录不受影响。"""

    _touch(tmp_path / "a" / "b" / "ignored.py")
    kept = _touch(tmp_path / "a" / "c" / "kept.py")
    rules = IgnoreRules(paths=frozenset({"a/b"}), workspace_root=tmp_path)

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

    rules = load_ignore_rules(tmp_path)

    assert rules.names == frozenset(DEFAULT_IGNORED_DIR_NAMES)
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

    rules = load_ignore_rules(tmp_path)

    assert rules.names == frozenset(DEFAULT_IGNORED_DIR_NAMES)
    assert not ignore_file_path(tmp_path).exists()
    assert "file_ignore_create_failed" in _log_events(caplog)
