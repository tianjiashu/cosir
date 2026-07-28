"""ListDirectoryTool 单元测试。

覆盖：默认隐藏项过滤、含隐藏项、与 os.scandir 行为一致性、排序规则、
execution_context 守卫、指向文件的错误、路径不存在错误、分页（含 next_offset /
total）、隐藏项与分页组合。
"""

import os
import tempfile
from pathlib import Path

import pytest

from app.tools.schemas import ToolExecutionContext, ToolObservation
from app.tools.tool_handler.list_directory import ListDirectoryTool
from app.tools.tool_models.list_directory_args import ListDirectoryArgs


def _make_context(root: Path) -> ToolExecutionContext:
    """构造一个指向临时目录的执行上下文。"""
    return ToolExecutionContext(task_id="t", workspace_id="w", workspace_root=root)


def _build_workspace(base: Path) -> Path:
    """在 base 下造一个混合子目录：普通文件、子目录、dot 文件/目录。

    返回被列举的目标子目录路径。结构：
      target/
        alpha.txt          (file)
        Bravo.md           (file)
        charlie/           (dir)
        Delta/             (dir)
        .git/              (dir)
        .env               (file)
        .hidden_file       (file)
    """
    target = base / "target"
    target.mkdir()

    (target / "alpha.txt").write_text("a")
    (target / "Bravo.md").write_text("b")
    (target / "charlie").mkdir()
    (target / "Delta").mkdir()
    (target / ".git").mkdir()
    (target / ".env").write_text("c")
    (target / ".hidden_file").write_text("d")
    return target


# ---------------------------------------------------------------------------
# 1. 默认 include_hidden=False：不含任何 dot 条目，普通文件/目录正常列出
# ---------------------------------------------------------------------------
# 测试目的：验证默认配置下 dot 条目被过滤，普通条目全部出现。
# 可能发现的缺陷类型：隐藏过滤逻辑失效（误把 dot 条目也列出）。
def test_default_excludes_hidden():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _build_workspace(root)
        ctx = _make_context(root)

        obs = ListDirectoryTool().execute(str(target), execution_context=ctx)

        assert obs.status == "success"
        names = {line.split()[-1] for line in obs.content.splitlines() if line.strip()}
        # 普通条目应出现
        assert {"alpha.txt", "Bravo.md", "charlie", "Delta"}.issubset(names)
        # dot 条目不应出现
        assert not any(n.startswith(".") for n in names)
        assert ".git" not in names and ".env" not in names and ".hidden_file" not in names


# ---------------------------------------------------------------------------
# 2. include_hidden=True：dot 条目出现在输出中（数量一致）
# ---------------------------------------------------------------------------
# 测试目的：验证 include_hidden=True 时 dot 条目被包含，且数量与构造一致。
# 可能发现的缺陷类型：隐藏开关被忽略 / dot 数量统计错误。
def test_include_hidden_shows_dot_entries():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _build_workspace(root)
        ctx = _make_context(root)

        obs = ListDirectoryTool().execute(
            str(target), include_hidden=True, execution_context=ctx
        )

        assert obs.status == "success"
        names = {line.split()[-1] for line in obs.content.splitlines() if line.strip()}
        dot_entries = {n for n in names if n.startswith(".")}
        assert dot_entries == {".git", ".env", ".hidden_file"}
        # total 应为 7（4 普通 + 3 dot）
        assert obs.data["total"] == 7


# ---------------------------------------------------------------------------
# 3. 与 os.scandir/os.listdir 对可见条目的集合一致；排序：目录在前，再按名称小写排序
# ---------------------------------------------------------------------------
# 测试目的：验证 scandir 重构后 listing 集合与 os.scandir 一致，且排序契约正确。
# 可能发现的缺陷类型：列举集合漂移 / 排序规则错误（未把目录排前、未按小写排序）。
def test_matches_scandir_and_sorting():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _build_workspace(root)
        ctx = _make_context(root)

        obs = ListDirectoryTool().execute(str(target), execution_context=ctx)
        assert obs.status == "success"

        # 与 os.scandir 对可见（非隐藏）条目的集合一致
        expected_visible = {
            e.name for e in os.scandir(target) if not e.name.startswith(".")
        }
        got = {line.split()[-1] for line in obs.content.splitlines() if line.strip()}
        assert got == expected_visible

        # 排序：目录在前，再按名称小写排序
        entries = [line.split()[-1] for line in obs.content.splitlines() if line.strip()]
        is_dir = {e.name: e.is_dir() for e in os.scandir(target) if not e.name.startswith(".")}
        dirs = [n for n in entries if is_dir[n]]
        files = [n for n in entries if not is_dir[n]]
        assert dirs == sorted(dirs, key=str.lower)
        assert files == sorted(files, key=str.lower)
        # 所有目录都出现在所有文件之前
        first_file_idx = min(entries.index(f) for f in files)
        last_dir_idx = max(entries.index(d) for d in dirs)
        assert last_dir_idx < first_file_idx


# ---------------------------------------------------------------------------
# 4. execution_context=None：返回 ToolObservation 且 status=="error"，不抛异常
# ---------------------------------------------------------------------------
# 测试目的：验证守卫分支生效，None 上下文被归一化为 error 观察而非抛异常。
# 可能发现的缺陷类型：守卫缺失导致 AttributeError 抛出 / 未返回观察。
def test_none_execution_context_returns_error():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _build_workspace(root)

        # 直接调用，不传 execution_context（默认 None）
        obs = ListDirectoryTool().execute(str(target))

        assert isinstance(obs, ToolObservation)
        assert obs.status == "error"
        assert obs.error  # 应带有错误说明


# ---------------------------------------------------------------------------
# 5. 路径指向一个文件（非目录）：status=="error"，reason 指向 "it is a file"
# ---------------------------------------------------------------------------
# 测试目的：验证文件而非目录时返回结构化 error，且 error 文本含 "it is a file, not a directory"。
# 可能发现的缺陷类型：文件误判 / 错误信息未指向文件类型。
def test_path_is_file_returns_error():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _build_workspace(root)
        ctx = _make_context(root)
        file_path = target / "alpha.txt"

        obs = ListDirectoryTool().execute(str(file_path), execution_context=ctx)

        assert obs.status == "error"
        assert "it is a file, not a directory" in obs.error


# ---------------------------------------------------------------------------
# 6. 路径不存在：status=="error"，reason 指向不存在
# ---------------------------------------------------------------------------
# 测试目的：验证不存在路径返回 error，且 reason 指向 "does not exist"。
# 可能发现的缺陷类型：不存在路径被误判为其他错误 / 未返回 error。
def test_path_not_exist_returns_error():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ctx = _make_context(root)
        missing = root / "does_not_exist_xyz"

        obs = ListDirectoryTool().execute(str(missing), execution_context=ctx)

        assert obs.status == "error"
        assert "does not exist" in obs.reason


# ---------------------------------------------------------------------------
# 7. 分页：offset/limit 生效；next_offset 截断时为整数、未截断为 None；total 为过滤后总数
# ---------------------------------------------------------------------------
# 测试目的：验证分页契约（offset/limit/next_offset/total）。
# 可能发现的缺陷类型：分页切片错误 / next_offset 计算错误（应为 None 时却给整数）。
def test_pagination_basic():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _build_workspace(root)
        ctx = _make_context(root)

        # 仅 4 个可见条目（不含 dot），用 limit=2 触发截断
        obs = ListDirectoryTool().execute(
            str(target), offset=0, limit=2, execution_context=ctx
        )
        assert obs.status == "success"
        # content 末尾可能含截断提示行，仅统计条目行（含 " 大小  mtime  名称" 四段）
        names_page1 = [
            l.split()[-1]
            for l in obs.content.splitlines()
            if l.strip() and l.split()[0] in ("dir", "file")
        ]
        assert len(names_page1) == 2
        assert obs.data["total"] == 4
        assert obs.data["offset"] == 0
        assert obs.data["limit"] == 2
        assert isinstance(obs.data["next_offset"], int)
        # 截断提示应写入 content
        assert "truncated" in obs.content

        # 取第二页
        obs2 = ListDirectoryTool().execute(
            str(target), offset=2, limit=2, execution_context=ctx
        )
        names_page2 = [
            l.split()[-1]
            for l in obs2.content.splitlines()
            if l.strip() and l.split()[0] in ("dir", "file")
        ]
        assert len(names_page2) == 2
        assert obs2.data["next_offset"] is None  # 未再截断
        assert "truncated" not in obs2.content
        # 两页并集 == 全体可见条目
        assert set(names_page1) | set(names_page2) == {
            "alpha.txt", "Bravo.md", "charlie", "Delta"
        }


# ---------------------------------------------------------------------------
# 8. include_hidden=True 与分页组合：dot 条目计入 total 且参与分页
# ---------------------------------------------------------------------------
# 测试目的：验证开启隐藏项时 total 与分页都包含 dot 条目。
# 可能发现的缺陷类型：分页在过滤前计算 / dot 条目未参与分页计数。
def test_hidden_with_pagination():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _build_workspace(root)
        ctx = _make_context(root)

        # 7 个条目（含 3 dot），limit=3 触发截断
        obs = ListDirectoryTool().execute(
            str(target), offset=0, limit=3, include_hidden=True, execution_context=ctx
        )
        assert obs.status == "success"
        assert obs.data["total"] == 7
        page1 = [
            l.split()[-1]
            for l in obs.content.splitlines()
            if l.strip() and l.split()[0] in ("dir", "file")
        ]
        assert len(page1) == 3
        assert isinstance(obs.data["next_offset"], int)

        # 第二页也应含 dot 条目（至少覆盖一次）
        seen = set(page1)
        off = obs.data["next_offset"]
        while off is not None:
            o = ListDirectoryTool().execute(
                str(target), offset=off, limit=3, include_hidden=True, execution_context=ctx
            )
            page = [
                l.split()[-1]
                for l in o.content.splitlines()
                if l.strip() and l.split()[0] in ("dir", "file")
            ]
            seen |= set(page)
            off = o.data["next_offset"]
        assert seen == {".git", ".env", ".hidden_file", "alpha.txt", "Bravo.md", "charlie", "Delta"}


# ---------------------------------------------------------------------------
# 额外：参数模型契约（ListDirectoryArgs）校验边界
# ---------------------------------------------------------------------------
# 测试目的：验证 ListDirectoryArgs 对非法参数的拒绝（strict + 边界约束）。
# 可能发现的缺陷类型：参数模型约束失效（如允许 limit<1 / 越界 offset）。
def test_list_directory_args_validation():
    # 合法默认
    args = ListDirectoryArgs(path="src")
    assert args.offset == 0 and args.limit == 200 and args.include_hidden is False

    # limit 越下界（<1）应被 pydantic 拒绝
    with pytest.raises(Exception):
        ListDirectoryArgs(path="x", limit=0)

    # limit 越上界（>500）应被拒绝
    with pytest.raises(Exception):
        ListDirectoryArgs(path="x", limit=501)

    # offset 为负应被拒绝
    with pytest.raises(Exception):
        ListDirectoryArgs(path="x", offset=-1)

    # strict 模式应拒绝类型错误（path 传 int）
    with pytest.raises(Exception):
        ListDirectoryArgs(path=123)


# ===========================================================================
# 以下为新增用例：ignore_globs 相关 + 字段名契约
# ===========================================================================
def _build_workspace_with_py(base: Path) -> Path:
    """在 base 下造一个混合工作区，含普通文件、子目录、dot 文件与 .py/.txt 文件。

    结构（target/）：
      a.py                (file)
      b.py                (file)
      c.txt               (file)
      sub/                (dir)
      .git/               (dir)
      .env                (file)
    返回被列举的目标目录路径。
    """
    target = base / "target"
    target.mkdir()
    (target / "a.py").write_text("a")
    (target / "b.py").write_text("b")
    (target / "c.txt").write_text("c")
    (target / "sub").mkdir()
    (target / ".git").mkdir()
    (target / ".env").write_text("e")
    return target


# ---------------------------------------------------------------------------
# 9+1. ignore_globs=["*.py"]：排除所有 .py 文件，其余非 dot 条目仍在
# ---------------------------------------------------------------------------
# 测试目的：验证 ignore_globs 按名称排除 *.py，total 与可见非 py 条目数一致。
# 可能发现的缺陷类型：glob 匹配失效 / 被排除条目仍计入 total。
def test_ignore_globs_excludes_py():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _build_workspace_with_py(root)
        ctx = _make_context(root)

        obs = ListDirectoryTool().execute(
            str(target), ignore_globs=["*.py"], execution_context=ctx
        )
        assert obs.status == "success"

        names = {
            l.split()[-1]
            for l in obs.content.splitlines()
            if l.strip() and l.split()[0] in ("dir", "file")
        }
        # 所有 .py 均被排除
        assert ".py" not in {n for n in names if n.endswith(".py")}
        assert "a.py" not in names and "b.py" not in names
        # 其余非 dot 条目仍在
        assert {"c.txt", "sub"}.issubset(names)
        # total 应为 2（c.txt + sub），与可见非 py 条目数一致
        assert obs.data["total"] == 2


# ---------------------------------------------------------------------------
# 9+2. ignore_globs=[".git", ".env"]：dot 条目被显式排除（等价隐藏）
# ---------------------------------------------------------------------------
# 测试目的：验证显式 glob 能排除 dot 条目；再验证 include_hidden=True
#          配合 ignore_globs=[".env"] 时 .env 仍被排除、其它 dot 出现。
# 可能发现的缺陷类型：ignore_globs 未对 dot 条目生效 / 与 include_hidden 未独立叠加。
def test_ignore_globs_excludes_dot_entries():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _build_workspace_with_py(root)
        ctx = _make_context(root)

        # 默认 include_hidden=False + ignore_globs 排除 .git/.env
        obs = ListDirectoryTool().execute(
            str(target), ignore_globs=[".git", ".env"], execution_context=ctx
        )
        assert obs.status == "success"
        names = {
            l.split()[-1]
            for l in obs.content.splitlines()
            if l.strip() and l.split()[0] in ("dir", "file")
        }
        assert ".git" not in names and ".env" not in names
        assert {"a.py", "b.py", "c.txt", "sub"}.issubset(names)

    # 独立叠加：include_hidden=True 但仍用 ignore_globs=[".env"]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _build_workspace_with_py(root)
        ctx = _make_context(root)

        obs = ListDirectoryTool().execute(
            str(target),
            include_hidden=True,
            ignore_globs=[".env"],
            execution_context=ctx,
        )
        assert obs.status == "success"
        names = {
            l.split()[-1]
            for l in obs.content.splitlines()
            if l.strip() and l.split()[0] in ("dir", "file")
        }
        # .env 仍被排除
        assert ".env" not in names
        # 其它 dot 条目（.git）出现，证明 include_hidden 生效且 ignore_globs 独立叠加
        assert ".git" in names
        assert {"a.py", "b.py", "c.txt", "sub"}.issubset(names)
        # 工作区共 6 个条目（a.py,b.py,c.txt,sub,.git,.env），排除 .env 后剩 5
        assert obs.data["total"] == 5


# ---------------------------------------------------------------------------
# 9+3. ignore_globs=[] 与 ignore_globs=None：行为等价，不排除任何可见条目
# ---------------------------------------------------------------------------
# 测试目的：验证空列表与 None 等价，均不排除可见条目。
# 可能发现的缺陷类型：空列表被误当作"排除所有" / None 分支逻辑错位。
def test_ignore_globs_empty_and_none_equivalent():
    built = {}

    def run(globs):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = _build_workspace_with_py(root)
            ctx = _make_context(root)
            obs = ListDirectoryTool().execute(
                str(target), ignore_globs=globs, execution_context=ctx
            )
            assert obs.status == "success"
            return {
                l.split()[-1]
                for l in obs.content.splitlines()
                if l.strip() and l.split()[0] in ("dir", "file")
            }

    none_names = run(None)
    empty_names = run([])
    # 等价：均包含全部可见非 dot 条目
    assert none_names == {"a.py", "b.py", "c.txt", "sub"}
    assert empty_names == {"a.py", "b.py", "c.txt", "sub"}


# ---------------------------------------------------------------------------
# 9+4. 多模式 ignore_globs=["*.py", "*.txt"]：两类均被排除
# ---------------------------------------------------------------------------
# 测试目的：验证多个 glob 模式同时生效，*.py 与 *.txt 均被排除。
# 可能发现的缺陷类型：多模式只匹配第一个 / 列表遍历短路。
def test_ignore_globs_multiple_patterns():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _build_workspace_with_py(root)
        ctx = _make_context(root)

        obs = ListDirectoryTool().execute(
            str(target), ignore_globs=["*.py", "*.txt"], execution_context=ctx
        )
        assert obs.status == "success"
        names = {
            l.split()[-1]
            for l in obs.content.splitlines()
            if l.strip() and l.split()[0] in ("dir", "file")
        }
        assert "a.py" not in names and "b.py" not in names and "c.txt" not in names
        # 仅剩 sub 目录
        assert names == {"sub"}


# ---------------------------------------------------------------------------
# 9+5. 字段名契约（锁住此前修过的 bug）：args 字段为 path（非 target_directory）
# ---------------------------------------------------------------------------
# 测试目的：验证 ListDirectoryArgs 字段名为 path 且含 ignore_globs，model_dump 后可
#          作为关键字参数注入 execute（等价于 ToolExecutor 的 handler(**arguments)）。
# 可能发现的缺陷类型：字段名回退为 target_directory 导致 **dump 注入 TypeError。
def test_field_name_contract_path_not_target_directory():
    # 直接断言模型字段名
    assert "path" in ListDirectoryArgs.model_fields
    assert "target_directory" not in ListDirectoryArgs.model_fields
    assert "ignore_globs" in ListDirectoryArgs.model_fields

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _build_workspace_with_py(root)
        ctx = _make_context(root)

        # model_dump 应含 "path" 键，不含 "target_directory"，并含 "ignore_globs"
        dump = ListDirectoryArgs(path=str(target), ignore_globs=["*.py"]).model_dump()
        assert "path" in dump
        assert "target_directory" not in dump
        assert "ignore_globs" in dump

        # 等价于 ToolExecutor 注入路径：handler(**arguments) 不抛 TypeError
        obs = ListDirectoryTool().execute(**dump, execution_context=ctx)
        assert obs.status == "success"
        names = {
            l.split()[-1]
            for l in obs.content.splitlines()
            if l.strip() and l.split()[0] in ("dir", "file")
        }
        assert "a.py" not in names and "b.py" not in names  # 被 ignore_globs 排除
        assert {"c.txt", "sub"}.issubset(names)


# ---------------------------------------------------------------------------
# 9+6. ignore_globs 与分页组合：被排除条目不计入 total、不参与分页切片
# ---------------------------------------------------------------------------
# 测试目的：验证被 ignore_globs 排除的条目不计入 total 且不参与分页。
# 可能发现的缺陷类型：total 在过滤前计算 / 分页切片包含已排除条目。
def test_ignore_globs_with_pagination():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _build_workspace_with_py(root)
        ctx = _make_context(root)

        # 可见非 dot 共 4（a.py,b.py,c.txt,sub），排除 *.py 后剩 2（c.txt,sub）
        # limit=1 触发分页，total 应为 2（过滤后），而非 4
        obs = ListDirectoryTool().execute(
            str(target), offset=0, limit=1, ignore_globs=["*.py"], execution_context=ctx
        )
        assert obs.status == "success"
        assert obs.data["total"] == 2
        assert obs.data["offset"] == 0
        assert obs.data["limit"] == 1
        assert isinstance(obs.data["next_offset"], int)
        page1 = [
            l.split()[-1]
            for l in obs.content.splitlines()
            if l.strip() and l.split()[0] in ("dir", "file")
        ]
        assert len(page1) == 1
        # 任何一页都不应出现被排除的 .py
        assert "a.py" not in page1 and "b.py" not in page1

        # 翻完所有页，并集正好是被排除后的 2 个条目（c.txt, sub）
        seen = set(page1)
        off = obs.data["next_offset"]
        while off is not None:
            o = ListDirectoryTool().execute(
                str(target), offset=off, limit=1, ignore_globs=["*.py"], execution_context=ctx
            )
            page = [
                l.split()[-1]
                for l in o.content.splitlines()
                if l.strip() and l.split()[0] in ("dir", "file")
            ]
            seen |= set(page)
            off = o.data["next_offset"]
        assert seen == {"c.txt", "sub"}

