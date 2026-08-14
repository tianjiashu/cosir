"""ApplyPatchTool（原 patch 工具 patch 模式）单元测试 + 文件级集成测试。

只测行为契约、边界与错误路径，不修改任何业务代码。
workspace 用 pytest tmp_path 构造 ToolExecutionContext（参照 conftest.py 的 context fixture）。
所有涉及磁盘的用例都在临时目录操作真实文件，用完由 pytest tmp_path 自动清理。
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.tools.schemas import ToolExecutionContext
from app.tools.tool_handler.apply_patch_tool import ApplyPatchTool, build_apply_patch_definition
from app.tools.tool_models.apply_patch_args import ApplyPatchArgs


# ----------------------------------------------------------------------
# 1. 参数模型校验：ApplyPatchArgs.patch 必填
# ----------------------------------------------------------------------


def test_apply_patch_args_requires_patch() -> None:
    # 测试目的：缺少 patch 字段时 pydantic 抛 ValidationError。
    # 可能发现的缺陷：patch 被误设为 optional。
    with pytest.raises(ValidationError):
        ApplyPatchArgs()


def test_apply_patch_args_rejects_extra_fields() -> None:
    # 测试目的：extra="forbid" 拒绝多余字段（如旧的 mode 字段残留）。
    # 可能发现的缺陷：extra 未设为 forbid，非法字段被静默吞掉。
    with pytest.raises(ValidationError):
        ApplyPatchArgs(patch="*** Begin Patch\n*** End Patch", mode="patch")


def test_apply_patch_args_rejects_wrong_types() -> None:
    # 测试目的：strict=True 拒绝类型错误入参。
    # 可能发现的缺陷：strict 未生效，类型被静默 coerce。
    with pytest.raises(ValidationError):
        ApplyPatchArgs(patch=123)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# 2. execute 成功路径：V4A 多文件补丁
# ----------------------------------------------------------------------


def _write(workspace: Path, rel: str, content: str) -> Path:
    path = workspace / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_apply_patch_execute_update_returns_diff(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：UPDATE 操作成功应用，返回 success + unified diff + diff_stats。
    # 可能发现的缺陷：成功却返回 error、diff 未生成、diff_stats 缺失。
    _write(workspace, "a.py", "def foo():\n    return 1\n")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.py\n"
        "@@\n"
        " def foo():\n"
        "-    return 1\n"
        "+    return 2\n"
        "*** End Patch\n"
    )
    tool = ApplyPatchTool()
    obs = tool.execute(context, patch=patch)
    assert obs.status == "success", obs.error
    assert "return 2" in obs.content
    assert obs.data is not None
    assert "diff_stats" in obs.data
    stats = obs.data["diff_stats"]
    assert stats["total_files"] == 1
    assert (workspace / "a.py").read_text(encoding="utf-8").count("return 2") == 1


def test_apply_patch_execute_add_file(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：ADD 操作创建新文件。
    # 可能发现的缺陷：新文件未被创建、或误报已存在。
    patch = (
        "*** Begin Patch\n"
        "*** Add File: new.py\n"
        "+print('new')\n"
        "*** End Patch\n"
    )
    tool = ApplyPatchTool()
    obs = tool.execute(context, patch=patch)
    assert obs.status == "success", obs.error
    # ADD 从 hunk '+' 行拼接（"\n".join），不保留尾部换行；这是 patch 引擎既有设计，
    # 有显式 content 字段时才整文件还原保留换行。断言须对齐该行为契约。
    assert (workspace / "new.py").read_text(encoding="utf-8") == "print('new')"


def test_apply_patch_execute_delete_file(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：DELETE 操作删除已存在文件。
    # 可能发现的缺陷：文件未被删除、或删不存在文件时误成功。
    _write(workspace, "gone.py", "x = 1\n")
    patch = (
        "*** Begin Patch\n"
        "*** Delete File: gone.py\n"
        "*** End Patch\n"
    )
    tool = ApplyPatchTool()
    obs = tool.execute(context, patch=patch)
    assert obs.status == "success", obs.error
    assert not (workspace / "gone.py").exists()


def test_apply_patch_execute_move_file(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：MOVE 操作重命名文件（content 不变，new_path 在 diff 中标记）。
    # 可能发现的缺陷：move 未生效、源仍存在或目标未建、diff 缺 Moved 标记。
    _write(workspace, "src.py", "data = 1\n")
    patch = (
        "*** Begin Patch\n"
        "*** Move File: src.py -> dst.py\n"
        "*** End Patch\n"
    )
    tool = ApplyPatchTool()
    obs = tool.execute(context, patch=patch)
    assert obs.status == "success", obs.error
    assert (workspace / "dst.py").exists()
    assert not (workspace / "src.py").exists()
    assert "Moved" in obs.content


def test_apply_patch_execute_multi_file_diff_stats(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：多文件补丁应用后 diff_stats.total_files 反映变更文件数。
    # 可能发现的缺陷：统计计数遗漏或重复。
    _write(workspace, "a.py", "a = 1\n")
    _write(workspace, "b.py", "b = 1\n")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.py\n"
        "@@\n"
        " a = 1\n"
        "+a = 2\n"
        "*** Update File: b.py\n"
        "@@\n"
        " b = 1\n"
        "+b = 2\n"
        "*** End Patch\n"
    )
    tool = ApplyPatchTool()
    obs = tool.execute(context, patch=patch)
    assert obs.status == "success", obs.error
    assert obs.data is not None
    assert obs.data["diff_stats"]["total_files"] == 2


# ----------------------------------------------------------------------
# 3. 失败分支
# ----------------------------------------------------------------------


def test_apply_patch_execute_empty_patch(context: ToolExecutionContext) -> None:
    # 测试目的：空 patch（None/空串）应被早退拦截。
    # 可能发现的缺陷：空 patch 未被拦截，导致异常或误成功。
    tool = ApplyPatchTool()
    for value in ["", None]:  # type: ignore[list-item]
        obs = tool.execute(context, patch=value)  # type: ignore[arg-type]
        assert obs.status == "error"
        assert obs.retryable is False


def test_apply_patch_execute_line_numbered_patch_rejected(context: ToolExecutionContext) -> None:
    # 测试目的：patch 文本含行号前缀污染应被拒绝（不落盘）。
    # 可能发现的缺陷：行号污染被解析并写入文件。
    patch = (
        "*** Begin Patch\n"
        "1| *** Update File: a.py\n"
        "2| @@\n"
        "3| -x\n"
        "4| +y\n"
        "*** End Patch\n"
    )
    tool = ApplyPatchTool()
    obs = tool.execute(context, patch=patch)
    assert obs.status == "error"
    assert "line-numbered" in obs.error.lower()


def test_apply_patch_execute_parse_failure(context: ToolExecutionContext) -> None:
    # 测试目的：格式非法 V4A（UPDATE 无 hunk）应 parse 失败并 error。
    # 可能发现的缺陷：非法 patch 被当成空操作成功、或抛异常。
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.py\n"  # 无 @@ hunk
        "*** End Patch\n"
    )
    tool = ApplyPatchTool()
    obs = tool.execute(context, patch=patch)
    assert obs.status == "error"
    assert "Parse error" in obs.error or "no hunks" in obs.error.lower()


def test_apply_patch_execute_empty_operations(context: ToolExecutionContext) -> None:
    # 测试目的：解析成功但无任何 operation（无 header）应报 "no operations"。
    # 可能发现的缺陷：空操作被当成成功、或抛异常。
    patch = "*** Begin Patch\n*** End Patch\n"
    tool = ApplyPatchTool()
    obs = tool.execute(context, patch=patch)
    assert obs.status == "error"
    assert "no operations" in obs.error.lower()


def test_apply_patch_execute_validate_failure_escapes_workspace(context: ToolExecutionContext) -> None:
    # 测试目的：validate 阶段越界路径应被拒绝（无文件被改）。
    # 可能发现的缺陷：越界路径未被拦截，文件被写到 workspace 外。
    patch = (
        "*** Begin Patch\n"
        "*** Add File: ../escape.py\n"
        "+x\n"
        "*** End Patch\n"
    )
    tool = ApplyPatchTool()
    obs = tool.execute(context, patch=patch)
    assert obs.status == "error"
    assert "validation failed" in obs.error.lower()
    assert not (context.workspace_root.parent / "escape.py").exists()


def test_apply_patch_execute_validate_failure_missing_file_for_delete(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：DELETE 一个不存在的文件应 validate 失败（none applied）。
    # 可能发现的缺陷：删不存在文件被当成成功。
    patch = (
        "*** Begin Patch\n"
        "*** Delete File: nope.py\n"
        "*** End Patch\n"
    )
    tool = ApplyPatchTool()
    obs = tool.execute(context, patch=patch)
    assert obs.status == "error"
    assert "validation failed" in obs.error.lower()


def test_apply_patch_execute_validate_failure_add_existing(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：ADD 一个已存在文件应 validate 失败（destination already exists）。
    # 可能发现的缺陷：覆盖已存在文件、或误报成功。
    _write(workspace, "exists.py", "old\n")
    patch = (
        "*** Begin Patch\n"
        "*** Add File: exists.py\n"
        "+new\n"
        "*** End Patch\n"
    )
    tool = ApplyPatchTool()
    obs = tool.execute(context, patch=patch)
    assert obs.status == "error"
    assert "validation failed" in obs.error.lower()
    # 原文件未被覆盖
    assert (workspace / "exists.py").read_text(encoding="utf-8") == "old\n"


def test_apply_patch_execute_apply_failure_retryable(context: ToolExecutionContext, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 测试目的：应用阶段写盘抛 OSError 应被捕获为 PatchApplyError，归一化为
    # error 观察且 retryable=True（瞬态，可重试）。
    # 可能发现的缺陷：apply 异常未被捕获、抛原始异常、或误报成功。
    _write(workspace, "a.py", "real = 1\n")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.py\n"
        "@@\n"
        " real = 1\n"
        "+real = 2\n"
        "*** End Patch\n"
    )

    from app.tools.tool_handler.patch import patch_apply as pa_module

    def _boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError("simulated write failure")

    monkeypatch.setattr(pa_module, "atomic_write_text", _boom)
    tool = ApplyPatchTool()
    obs = tool.execute(context, patch=patch)
    assert obs.status == "error"
    assert "apply failed" in obs.error.lower()
    assert obs.retryable is True


def test_apply_patch_execute_validate_hunk_not_found(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：UPDATE 的 hunk 上下文与文件实际内容不符，应在 validate 阶段确定性
    # 失败（无文件被改，retryable=False）。
    # 可能发现的缺陷：hunk 不匹配却被当成成功、或误标 retryable。
    _write(workspace, "a.py", "real content here\n")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.py\n"
        "@@\n"
        "-not in file\n"
        "+changed\n"
        "*** End Patch\n"
    )
    tool = ApplyPatchTool()
    obs = tool.execute(context, patch=patch)
    assert obs.status == "error"
    assert "validation failed" in obs.error.lower()
    assert obs.retryable is False
    # 原文件未被改动
    assert (workspace / "a.py").read_text(encoding="utf-8") == "real content here\n"


def test_apply_patch_execute_syntax_error_aggregated(context: ToolExecutionContext, workspace: Path) -> None:
    # 测试目的：落盘后逐文件语法检查聚合 error（.py 非法），带 syntax_errors 诊断。
    # 可能发现的缺陷：语法错误被忽略、或文件未写。
    _write(workspace, "a.py", "x = 1\n")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.py\n"
        "@@\n"
        " x = 1\n"
        "+def broken(:\n"
        "*** End Patch\n"
    )
    tool = ApplyPatchTool()
    obs = tool.execute(context, patch=patch)
    assert obs.status == "error"
    assert "syntax" in obs.error.lower()
    assert obs.data is not None
    assert "syntax_errors" in obs.data
    # 文件已写（error 驱动：先写后查）
    assert "def broken(:" in (workspace / "a.py").read_text(encoding="utf-8")


# ----------------------------------------------------------------------
# 4. to_definition / build_apply_patch_definition 契约
# ----------------------------------------------------------------------


def test_apply_patch_to_definition_fields() -> None:
    # 测试目的：to_definition 的 name/permission/resource_keys/display 正确。
    # 可能发现的缺陷：name 被误改、resource_keys 缺失、display 缺失。
    definition = build_apply_patch_definition()
    assert definition.name == "apply_patch"
    assert definition.permission == "file_write"
    assert definition.resource_keys == ("filesystem",)
    assert definition.display is not None
    assert definition.display.icon == "git-compare"
    assert definition.display.expand_layout == "diff"


def test_apply_patch_description_has_no_replace_mode_text() -> None:
    # 测试目的：description 不应再含 replace 模式（fuzzy find-and-replace）描述。
    # 可能发现的缺陷：拆分不彻底，描述串台。
    description = build_apply_patch_definition().description
    assert "fuzzy matching" not in description.lower()
    assert "find-and-replace" not in description.lower()
    assert "V4A" in description


def test_apply_patch_args_model_bound_to_tool() -> None:
    # 测试目的：工具 args_model 与 ApplyPatchArgs 一致。
    assert ApplyPatchTool().args_model is ApplyPatchArgs


# ----------------------------------------------------------------------
# 5. 对比测试：replace 与 apply_patch 的 to_definition 字段不串台
# ----------------------------------------------------------------------


def test_replace_and_apply_patch_definitions_distinct() -> None:
    # 测试目的：两工具 name/description 互不串台，各自 description 不含对方模式。
    # 可能发现的缺陷：拆分不彻底，name 或描述相互污染。
    from app.tools.tool_handler.replace_tool import build_replace_definition

    replace_def = build_replace_definition()
    patch_def = build_apply_patch_definition()

    assert replace_def.name == "patch"
    assert patch_def.name == "apply_patch"
    assert replace_def.name != patch_def.name
    # replace 不应描述 V4A；apply_patch 不应描述 fuzzy replace。
    assert "V4A" not in replace_def.description
    assert "fuzzy matching" not in patch_def.description.lower()
