"""ReplaceTool（原 patch 工具 replace 模式）单元测试 + 文件级集成测试。

只测行为契约、边界与错误路径，不修改任何业务代码。
workspace 用 pytest tmp_path 构造 ToolExecutionContext（参照 conftest.py 的 context fixture）。
所有涉及磁盘的用例都在临时目录操作真实文件，用完由 pytest tmp_path 自动清理。
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.tools.schemas import ToolExecutionContext
from app.tools.tool_handler.replace_tool import ReplaceTool, build_replace_definition
from app.tools.tool_models.replace_args import ReplaceArgs


# ----------------------------------------------------------------------
# 1. 参数模型校验：ReplaceArgs 必填字段（path / old_string / new_string）
# ----------------------------------------------------------------------


def test_replace_args_requires_all_fields() -> None:
    # 测试目的：验证 path / old_string / new_string 任一缺失时 pydantic 抛 ValidationError。
    # 可能发现的缺陷：strict=Config 未强制必填、或字段被误设为 optional。
    for kwargs in [
        {},  # 全缺
        {"path": "a.py"},  # 缺 old_string / new_string
        {"path": "a.py", "old_string": "x"},  # 缺 new_string
        {"old_string": "x", "new_string": "y"},  # 缺 path
    ]:
        with pytest.raises(ValidationError):
            ReplaceArgs(**kwargs)


def test_replace_args_rejects_extra_fields() -> None:
    # 测试目的：验证 extra="forbid" 拒绝多余字段（如旧的 mode 字段残留）。
    # 可能发现的缺陷：extra 未设为 forbid，导致非法字段被静默吞掉。
    with pytest.raises(ValidationError):
        ReplaceArgs(path="a.py", old_string="x", new_string="y", mode="patch")


def test_replace_args_rejects_wrong_types() -> None:
    # 测试目的：验证 strict=True 拒绝类型错误的入参（如 path 传 int）。
    # 可能发现的缺陷：strict 未生效，类型被静默 coerce。
    with pytest.raises(ValidationError):
        ReplaceArgs(path=123, old_string="x", new_string="y")  # type: ignore[arg-type]


def test_replace_args_replace_all_defaults_false() -> None:
    # 测试目的：验证 replace_all 缺省为 False。
    # 可能发现的缺陷：默认被误设为 True。
    args = ReplaceArgs(path="a.py", old_string="x", new_string="y")
    # replace_all 是 Optional 模型字段，真实对象在 execute 中另有默认值。
    assert args.model_dump().get("replace_all", False) is False


# ----------------------------------------------------------------------
# 2. execute 成功路径
# ----------------------------------------------------------------------


def _write(workspace: Path, rel: str, content: str) -> Path:
    path = workspace / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_replace_execute_unique_hit_returns_diff(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：唯一命中时返回 success、content 含 unified diff、data 含 diff_stats。
    # 可能发现的缺陷：成功却返回 error、diff 未生成、diff_stats 缺失。
    _write(workspace, "a.py", "def foo():\n    return 1\n")
    tool = ReplaceTool()
    obs = tool.execute(context, path="a.py", old_string="return 1", new_string="return 2")
    assert obs.status == "success", obs.error
    assert "return 2" in obs.content
    assert obs.data is not None
    assert "diff_stats" in obs.data
    stats = obs.data["diff_stats"]
    assert stats["total_files"] == 1
    assert stats["total_insertions"] >= 1
    assert stats["total_deletions"] >= 1


def test_replace_execute_replace_all(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：replace_all=True 替换所有命中。
    # 可能发现的缺陷：replace_all 未生效，只换第一处或报错。
    _write(workspace, "a.py", "x = 1\nx = 1\n")
    tool = ReplaceTool()
    obs = tool.execute(context, path="a.py", old_string="x = 1", new_string="x = 2", replace_all=True)
    assert obs.status == "success", obs.error
    assert (workspace / "a.py").read_text(encoding="utf-8") == "x = 2\nx = 2\n"


def test_replace_execute_fuzzy_match_tolerates_whitespace(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：模糊匹配容忍空白差异（line_trimmed 策略）。
    # 可能发现的缺陷：严格裸匹配导致本应成功的替换失败。
    _write(workspace, "a.py", "def foo():\n    print('hi')\n")
    tool = ReplaceTool()
    obs = tool.execute(
        context,
        path="a.py",
        old_string="def foo():\nprint('hi')",
        new_string="def bar():\n    print('hi')",
    )
    assert obs.status == "success", obs.error
    assert "def bar()" in (workspace / "a.py").read_text(encoding="utf-8")


def test_replace_execute_atomic_write_mutates_file(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：确认原子写后磁盘文件确实被修改。
    # 可能发现的缺陷：只返回 success 但实际未落盘。
    _write(workspace, "a.py", "original\n")
    tool = ReplaceTool()
    obs = tool.execute(context, path="a.py", old_string="original", new_string="changed")
    assert obs.status == "success", obs.error
    assert (workspace / "a.py").read_text(encoding="utf-8") == "changed\n"


# ----------------------------------------------------------------------
# 3. 失败分支
# ----------------------------------------------------------------------


def test_replace_execute_old_equals_new(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：old_string == new_string 应确定性失败（no-op）。
    # 可能发现的缺陷：被当成成功、或产生空 diff 误报成功。
    _write(workspace, "a.py", "aaa\n")
    tool = ReplaceTool()
    obs = tool.execute(context, path="a.py", old_string="aaa", new_string="aaa")
    assert obs.status == "error"
    assert "identical" in obs.error.lower()
    assert obs.retryable is False


def test_replace_execute_not_found(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：old_string 不存在应失败。
    # 可能发现的缺陷：返回 success（虚假成功）。
    _write(workspace, "a.py", "hello\n")
    tool = ReplaceTool()
    obs = tool.execute(context, path="a.py", old_string="nonexistent", new_string="x")
    assert obs.status == "error"
    assert obs.error
    assert obs.retryable is False


def test_replace_execute_multiple_matches_without_replace_all(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：非唯一命中且未置 replace_all 应失败（要求唯一）。
    # 可能发现的缺陷：多命中被悄悄替换或全部替换，未报错。
    _write(workspace, "a.py", "dup\ndup\n")
    tool = ReplaceTool()
    obs = tool.execute(context, path="a.py", old_string="dup", new_string="x", replace_all=False)
    assert obs.status == "error"
    assert obs.retryable is False


def test_replace_execute_path_escapes_workspace(context: ToolExecutionContext) -> None:
    # 测试目的：绝对越界路径应被 PathResolver 拒绝（workspace escape）。
    # 可能发现的缺陷：越界路径未被拦截，文件被写到 workspace 外。
    tool = ReplaceTool()
    obs = tool.execute(context, path="../escape.py", old_string="a", new_string="b")
    assert obs.status == "error"
    assert "workspace" in obs.error.lower() or "escapes" in obs.error.lower()
    assert obs.retryable is False


def test_replace_execute_blocked_device_windows(context: ToolExecutionContext) -> None:
    # 测试目的：Windows 设备名（NUL）应被 blocked_device_reason 拒绝。
    # 可能发现的缺陷：设备名被当普通文件名处理。
    tool = ReplaceTool()
    obs = tool.execute(context, path="NUL", old_string="a", new_string="b")
    assert obs.status == "error"
    assert "device" in obs.error.lower()


def test_replace_execute_line_numbered_content_rejected(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：new_string 含行号前缀（read_file 输出污染）应被拒绝。
    # 可能发现的缺陷：行号污染被写入文件。
    _write(workspace, "a.py", "line one\n")
    tool = ReplaceTool()
    obs = tool.execute(
        context,
        path="a.py",
        old_string="line one",
        new_string="1| line one\n2| line two",
    )
    assert obs.status == "error"
    assert "line-numbered" in obs.error.lower() or "N| " in obs.error


def test_replace_execute_syntax_error_after_patch(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：落盘后发现语法错误应返回 error 观察 + reason 引导（文件已写）。
    # 可能发现的缺陷：语法错误被忽略、或文件未写。
    _write(workspace, "a.py", "x = 1\n")
    tool = ReplaceTool()
    obs = tool.execute(context, path="a.py", old_string="x = 1", new_string="def f(:")
    assert obs.status == "error"
    assert "syntax" in obs.error.lower()
    assert obs.reason
    # 文件应已被写入（error 驱动：先写后查）。
    assert "def f(:" in (workspace / "a.py").read_text(encoding="utf-8")
    assert obs.data is not None
    assert "syntax_errors" in obs.data


def test_replace_execute_missing_file_read_error(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：目标文件不存在时 OSError 应被归一化为 error 观察（retryable）。
    # 可能发现的缺陷：抛异常而非返回 error 观察。
    tool = ReplaceTool()
    obs = tool.execute(context, path="missing.py", old_string="a", new_string="b")
    assert obs.status == "error"
    assert obs.retryable is True


def test_replace_execute_empty_path(context: ToolExecutionContext) -> None:
    # 测试目的：path 为空字符串应被早退拦截。
    # 可能发现的缺陷：空 path 未被拦截导致异常或越界。
    tool = ReplaceTool()
    obs = tool.execute(context, path="", old_string="a", new_string="b")
    assert obs.status == "error"
    assert obs.retryable is False


def test_replace_execute_write_oserror_is_retryable(context: ToolExecutionContext, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 测试目的：原子写盘阶段抛 OSError 应被归一化为 error 观察且 retryable=True（瞬态）。
    # 可能发现的缺陷：write 异常未被捕获、抛原始异常、或误报成功。
    _write(workspace, "a.py", "x = 1\n")
    from app.tools.tool_handler import replace_tool as rt_module

    def _boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError("simulated write failure")

    monkeypatch.setattr(rt_module, "atomic_write_text", _boom)
    tool = ReplaceTool()
    obs = tool.execute(context, path="a.py", old_string="x = 1", new_string="x = 2")
    assert obs.status == "error"
    assert obs.retryable is True


# ----------------------------------------------------------------------
# 4. to_definition / build_replace_definition 契约
# ----------------------------------------------------------------------


def test_replace_to_definition_fields() -> None:
    # 测试目的：to_definition 的 name/permission/resource_keys/display 正确。
    # 可能发现的缺陷：name 被误改、resource_keys 缺失、display 缺失。
    definition = build_replace_definition()
    assert definition.name == "patch"
    assert definition.permission == "file_write"
    assert definition.resource_keys == ("filesystem",)
    assert definition.display is not None
    assert definition.display.icon == "git-compare"
    assert definition.display.expand_layout == "diff"


def test_replace_description_has_no_patch_mode_text() -> None:
    # 测试目的：description 不应再含 apply_patch 的 V4A/patch mode 描述。
    # 可能发现的缺陷：拆分不彻底，描述串台。
    description = build_replace_definition().description
    assert "V4A" not in description
    assert "apply_patch" not in description.lower()
    assert "REPLACE MODE" in description


def test_replace_args_model_bound_to_tool() -> None:
    # 测试目的：工具 args_model 与 ReplaceArgs 一致。
    assert ReplaceTool().args_model is ReplaceArgs
