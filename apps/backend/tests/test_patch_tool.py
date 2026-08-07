"""patch 工具（replace + patch 两种模式）的严苛单元测试。

覆盖 ``PatchTool.execute`` 全部分支及其依赖（PathResolver / fuzzy_match /
patch_parser / patch_apply / patch_diff / file_change_display / syntax_check），
并针对潜在 bug 设计对抗性用例：
- 未知 mode / 必填缺失 / no-op（old==new）拦截；
- 无匹配 / 多命中无 replace_all / replace_all 命中；
- 模糊匹配（缩进不敏感）；
- 行号污染门禁（replace 结果 & patch 文本）；
- V4A add/update/delete/move 全操作，含校验失败整体不落盘；
- patch 部分应用不回滚（已知限制的行为固化）；
- 语法错误后文件已写但返回 error（单文件 & 多文件聚合）；
- ``_format_multi_file_syntax_reason`` 首参命名为 ``c`` 的坏味道（行为仍正确）；
- OSError 归一化（retryable）。
"""

import pytest

from app.tools.tool_handler.patch.patch_parser import parse_v4a_patch
from app.tools.tool_handler.patch_tool import (
    PatchTool,
    build_patch_definition,
)


@pytest.fixture
def tool() -> PatchTool:
    return PatchTool()


def _wrap(body: str) -> str:
    """把 V4A 操作体包成完整 patch。"""

    return "*** Begin Patch\n" + body + "\n*** End Patch\n"


# --------------------------------------------------------------------------- #
# mode 分流
# --------------------------------------------------------------------------- #


def test_unknown_mode_rejected(tool, context):
    obs = tool.execute(execution_context=context, mode="bogus")

    assert obs.status == "error"
    assert "unknown mode: bogus" in obs.error


# --------------------------------------------------------------------------- #
# replace 模式：参数校验
# --------------------------------------------------------------------------- #


def test_replace_missing_args_rejected(tool, context):
    obs = tool.execute(execution_context=context, mode="replace", path="a.txt")
    assert obs.status == "error"
    assert "requires path, old_string, new_string" in obs.error


def test_replace_empty_string_new_string_is_valid(tool, context, workspace):
    """new_string='' 是合法删除语义，不应被 missing-arg 分支误拒。"""

    target = workspace / "d.txt"
    target.write_text("keep DELETME tail", encoding="utf-8")

    obs = tool.execute(
        execution_context=context,
        mode="replace",
        path="d.txt",
        old_string="DELETME ",
        new_string="",
    )
    assert obs.status == "success"
    assert target.read_text(encoding="utf-8") == "keep tail"


def test_replace_identical_old_new_rejected(tool, context, workspace):
    (workspace / "s.txt").write_text("abc", encoding="utf-8")
    obs = tool.execute(
        execution_context=context,
        mode="replace",
        path="s.txt",
        old_string="abc",
        new_string="abc",
    )
    assert obs.status == "error"
    assert "identical" in obs.error


def test_replace_device_path_rejected(tool, context):
    obs = tool.execute(
        execution_context=context,
        mode="replace",
        path="CON",
        old_string="a",
        new_string="b",
    )
    assert obs.status == "error"
    assert "device" in obs.error.lower()


def test_replace_path_escape_rejected(tool, context):
    obs = tool.execute(
        execution_context=context,
        mode="replace",
        path="../evil.txt",
        old_string="a",
        new_string="b",
    )
    assert obs.status == "error"
    assert "could not patch the file" in obs.error


# --------------------------------------------------------------------------- #
# replace 模式：匹配行为
# --------------------------------------------------------------------------- #


def test_replace_exact_success(tool, context, workspace):
    target = workspace / "f.txt"
    target.write_text("alpha beta gamma", encoding="utf-8")

    obs = tool.execute(
        execution_context=context,
        mode="replace",
        path="f.txt",
        old_string="beta",
        new_string="BETA",
    )
    assert obs.status == "success"
    assert target.read_text(encoding="utf-8") == "alpha BETA gamma"
    # content 是 unified diff 回显，data 含 diff_stats。
    assert "beta" in obs.content or "BETA" in obs.content
    assert obs.data["diff_stats"]["total_files"] == 1


def test_replace_no_match(tool, context, workspace):
    (workspace / "f.txt").write_text("hello", encoding="utf-8")
    obs = tool.execute(
        execution_context=context,
        mode="replace",
        path="f.txt",
        old_string="nonexistent",
        new_string="x",
    )
    assert obs.status == "error"
    assert "not found" in obs.reason.lower() or "not found" in obs.error.lower()


def test_replace_multiple_matches_without_replace_all(tool, context, workspace):
    (workspace / "f.txt").write_text("x x x", encoding="utf-8")
    obs = tool.execute(
        execution_context=context,
        mode="replace",
        path="f.txt",
        old_string="x",
        new_string="y",
    )
    assert obs.status == "error"
    assert "matches" in obs.error.lower()


def test_replace_all_success(tool, context, workspace):
    target = workspace / "f.txt"
    target.write_text("x x x", encoding="utf-8")
    obs = tool.execute(
        execution_context=context,
        mode="replace",
        path="f.txt",
        old_string="x",
        new_string="y",
        replace_all=True,
    )
    assert obs.status == "success"
    assert target.read_text(encoding="utf-8") == "y y y"


def test_replace_fuzzy_indentation(tool, context, workspace):
    """缩进差异应被模糊匹配容忍（indentation_flexible 策略）。"""

    target = workspace / "code.txt"
    target.write_text("    return foo()\n", encoding="utf-8")
    obs = tool.execute(
        execution_context=context,
        mode="replace",
        path="code.txt",
        old_string="return foo()",
        new_string="return bar()",
    )
    assert obs.status == "success"
    assert "bar()" in target.read_text(encoding="utf-8")


def test_replace_read_oserror_retryable(tool, context, workspace, monkeypatch):
    from pathlib import Path

    (workspace / "f.txt").write_text("data", encoding="utf-8")

    def boom(*a, **k):
        raise OSError("locked")

    monkeypatch.setattr(Path, "read_text", boom)
    obs = tool.execute(
        execution_context=context,
        mode="replace",
        path="f.txt",
        old_string="data",
        new_string="new",
    )
    assert obs.status == "error"
    assert obs.retryable is True


def test_replace_write_oserror_retryable(tool, context, workspace, monkeypatch):
    import app.tools.tool_handler.patch_tool as pt

    (workspace / "f.txt").write_text("data", encoding="utf-8")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(pt, "atomic_write_text", boom)
    obs = tool.execute(
        execution_context=context,
        mode="replace",
        path="f.txt",
        old_string="data",
        new_string="new",
    )
    assert obs.status == "error"
    assert obs.retryable is True


def test_replace_result_line_numbered_rejected(tool, context, workspace):
    """替换结果若像行号污染应被拒（new_string 引入 N| 前缀）。"""

    target = workspace / "f.txt"
    target.write_text("marker", encoding="utf-8")
    polluted_new = "1| a\n2| b\n3| c\n"
    obs = tool.execute(
        execution_context=context,
        mode="replace",
        path="f.txt",
        old_string="marker",
        new_string=polluted_new,
    )
    assert obs.status == "error"
    assert "line-numbered" in obs.error


def test_replace_syntax_error_written_but_error(tool, context, workspace):
    target = workspace / "s.py"
    target.write_text("x = 1\n", encoding="utf-8")
    obs = tool.execute(
        execution_context=context,
        mode="replace",
        path="s.py",
        old_string="x = 1",
        new_string="def broken(:",
    )
    assert obs.status == "error"
    assert obs.error == "syntax error detected after patch"
    # 文件已写入坏内容。
    assert "def broken(:" in target.read_text(encoding="utf-8")
    assert "syntax_errors" in obs.data


# --------------------------------------------------------------------------- #
# patch 模式：参数与解析
# --------------------------------------------------------------------------- #


def test_patch_empty_rejected(tool, context):
    obs = tool.execute(execution_context=context, mode="patch", patch="")
    assert obs.status == "error"
    assert "requires patch" in obs.error


def test_patch_line_numbered_rejected(tool, context):
    polluted = "1| *** Begin Patch\n2| *** Add File: a.txt\n3| +hi\n"
    obs = tool.execute(execution_context=context, mode="patch", patch=polluted)
    assert obs.status == "error"
    assert "line-numbered" in obs.error


def test_patch_parse_error(tool, context):
    # UPDATE 无 hunk -> 解析错误。
    body = "*** Update File: a.txt"
    obs = tool.execute(execution_context=context, mode="patch", patch=_wrap(body))
    assert obs.status == "error"
    assert "could not be parsed" in obs.reason


def test_patch_no_operations(tool, context):
    """有 Begin/End 但无操作 -> parse 成功但 operations 为空。"""

    obs = tool.execute(
        execution_context=context,
        mode="patch",
        patch="*** Begin Patch\n*** End Patch\n",
    )
    assert obs.status == "error"
    assert "no operations" in obs.error


# --------------------------------------------------------------------------- #
# patch 模式：应用
# --------------------------------------------------------------------------- #


def test_patch_add_file(tool, context, workspace):
    body = "*** Add File: created.txt\n+line one\n+line two"
    obs = tool.execute(execution_context=context, mode="patch", patch=_wrap(body))
    assert obs.status == "success"
    assert (workspace / "created.txt").read_text(encoding="utf-8") == "line one\nline two"
    assert obs.data["diff_stats"]["total_files"] == 1


def test_patch_add_existing_file_validation_fails(tool, context, workspace):
    (workspace / "dup.txt").write_text("exists", encoding="utf-8")
    body = "*** Add File: dup.txt\n+new"
    obs = tool.execute(execution_context=context, mode="patch", patch=_wrap(body))
    assert obs.status == "error"
    assert "validation failed" in obs.error
    # 校验失败整体不落盘：原文件保持不变。
    assert (workspace / "dup.txt").read_text(encoding="utf-8") == "exists"


def test_patch_update_file(tool, context, workspace):
    target = workspace / "u.txt"
    target.write_text("first\nsecond\nthird\n", encoding="utf-8")
    body = "*** Update File: u.txt\n@@\n first\n-second\n+SECOND\n third"
    obs = tool.execute(execution_context=context, mode="patch", patch=_wrap(body))
    assert obs.status == "success"
    assert "SECOND" in target.read_text(encoding="utf-8")


def test_patch_delete_file(tool, context, workspace):
    target = workspace / "gone.txt"
    target.write_text("bye", encoding="utf-8")
    body = "*** Delete File: gone.txt"
    obs = tool.execute(execution_context=context, mode="patch", patch=_wrap(body))
    assert obs.status == "success"
    assert not target.exists()


def test_patch_delete_missing_file_validation_fails(tool, context):
    body = "*** Delete File: nope.txt"
    obs = tool.execute(execution_context=context, mode="patch", patch=_wrap(body))
    assert obs.status == "error"
    assert "validation failed" in obs.error


def test_patch_move_file(tool, context, workspace):
    src = workspace / "src.txt"
    src.write_text("content", encoding="utf-8")
    body = "*** Move File: src.txt -> dst.txt"
    obs = tool.execute(execution_context=context, mode="patch", patch=_wrap(body))
    assert obs.status == "success"
    assert not src.exists()
    assert (workspace / "dst.txt").read_text(encoding="utf-8") == "content"


def test_patch_move_missing_destination_parse_error(tool, context, workspace):
    (workspace / "src.txt").write_text("c", encoding="utf-8")
    body = "*** Move File: src.txt"
    obs = tool.execute(execution_context=context, mode="patch", patch=_wrap(body))
    assert obs.status == "error"
    assert "could not be parsed" in obs.reason


def test_patch_multi_file_all_or_nothing_on_validation(tool, context, workspace):
    """一个操作校验失败，则所有操作都不落盘。"""

    (workspace / "ok.txt").write_text("x", encoding="utf-8")
    (workspace / "exists.txt").write_text("y", encoding="utf-8")
    body = (
        "*** Add File: brand_new.txt\n+data\n"
        "*** Add File: exists.txt\n+conflict"
    )
    obs = tool.execute(execution_context=context, mode="patch", patch=_wrap(body))
    assert obs.status == "error"
    # 第一个 add 也不应落盘（validate_all 先整体校验）。
    assert not (workspace / "brand_new.txt").exists()


def test_patch_syntax_error_multi_file_aggregated(tool, context, workspace):
    """两个 .py 文件都写坏，聚合诊断并返回 error（触发多文件 reason）。"""

    body = (
        "*** Add File: bad1.py\n+def broken(:\n"
        "*** Add File: bad2.py\n+class X(:"
    )
    obs = tool.execute(execution_context=context, mode="patch", patch=_wrap(body))
    assert obs.status == "error"
    assert obs.error == "syntax error detected in patched file(s)"
    # 文件确实已写（副作用），且诊断按文件维度聚合。
    assert (workspace / "bad1.py").exists()
    assert (workspace / "bad2.py").exists()
    syntax_errors = obs.data["syntax_errors"]
    assert len(syntax_errors) == 2
    paths = {e["path"] for e in syntax_errors}
    assert paths == {"bad1.py", "bad2.py"}
    # reason 应包含逐错误位置说明。
    assert "syntax error" in obs.reason.lower()


def test_patch_apply_error_retryable(tool, context, workspace, monkeypatch):
    """apply 阶段抛 PatchApplyError -> retryable=True。"""

    import app.tools.tool_handler.patch_tool as pt
    from app.tools.tool_handler.patch import PatchApplyError

    def boom(operations, resolver):
        raise PatchApplyError("write blew up", partial_applied=True)

    monkeypatch.setattr(pt, "apply_all_with_diff", boom)
    body = "*** Add File: n.txt\n+data"
    obs = tool.execute(execution_context=context, mode="patch", patch=_wrap(body))
    assert obs.status == "error"
    assert obs.retryable is True
    assert "already applied" in obs.reason


# --------------------------------------------------------------------------- #
# _format_multi_file_syntax_reason 直接单测（首参命名坏味道验证）
# --------------------------------------------------------------------------- #


def test_format_multi_file_syntax_reason_empty(tool):
    """空诊断走兜底分支。"""

    # 首参名为 c（应为 self），此处以实例方法调用验证行为仍正确。
    result = tool._format_multi_file_syntax_reason([])
    assert "syntax errors" in result
    assert "write_file or patch_tool" in result


def test_format_multi_file_syntax_reason_with_diagnostics(tool):
    from app.tools.guard.syntax_check import SyntaxDiagnostic

    diags = [
        SyntaxDiagnostic(
            language="python", row=3, column=10, kind="missing",
            message="missing ')'", expected=")",
        ),
        SyntaxDiagnostic(
            language="python", row=5, column=2, kind="unexpected",
            message="unexpected token",
        ),
    ]
    result = tool._format_multi_file_syntax_reason(diags)
    assert "line 3 col 10 missing ')'" in result
    assert "line 5 col 2 unexpected token" in result


# --------------------------------------------------------------------------- #
# 定义契约
# --------------------------------------------------------------------------- #


def test_to_definition_contract(tool):
    definition = tool.to_definition()
    assert definition.name == "patch"
    assert definition.permission == "file_write"
    assert definition.args_model.__name__ == "PatchArgs"
    assert definition.resource_keys == ("filesystem",)
    assert definition.display.expand_layout == "diff"


def test_build_patch_definition_factory():
    assert build_patch_definition().name == "patch"


# --------------------------------------------------------------------------- #
# 依赖：V4A 解析器补充边界（供 patch 工具直接依赖）
# --------------------------------------------------------------------------- #


def test_parse_v4a_empty_returns_no_operations():
    ops, err = parse_v4a_patch("")
    assert ops == []
    assert err is None


def test_parse_v4a_add_and_update_together():
    patch = (
        "*** Begin Patch\n"
        "*** Add File: a.txt\n+hello\n"
        "*** Update File: b.txt\n@@\n old\n-old\n+new\n"
        "*** End Patch\n"
    )
    ops, err = parse_v4a_patch(patch)
    assert err is None
    assert len(ops) == 2
    assert ops[0].operation.value == "add"
    assert ops[1].operation.value == "update"
