from pathlib import Path

import pytest

from app.core.tool.schemas import ToolExecutionContext
from app.core.tool.tool_base import HandlerBase
from app.tools.tool_handler.apply_patch_tool import ApplyPatchTool
from app.tools.tool_handler.patch_write import parse_git_unified_diff
from app.tools.tool_models.apply_patch_args import ApplyPatchArgs
from app.tools.tool_registration import build_tool_registry

# 工具描述的事实源已从模块级常量 ``APPLY_PATCH_DESCRIPTION`` 迁移为类属性
# ``ApplyPatchTool.description``（模块不再导出该名字）。这里只做「测试内别名」，把下文的
# 断言指向新位置，不要求 app 重新导出旧符号。
APPLY_PATCH_DESCRIPTION = ApplyPatchTool.description


def _context(root: Path) -> ToolExecutionContext:
    return ToolExecutionContext(step_id="step-1", session_id=1, workspace_id=1, workspace_root=root, run_id=991)


def _patch(path: str = "existing.txt") -> str:
    return "\n".join(
        (
            f"diff --git a/{path} b/{path}",
            f"--- a/{path}",
            f"+++ b/{path}",
            "@@ -1 +1 @@",
            "-before",
            "+after",
        )
    )


def test_file_tool_descriptions_and_arguments_express_separate_responsibilities() -> None:
    patch_description = ApplyPatchArgs.model_fields["patch"].description or ""

    # 工具描述只讲「这个工具做什么 / 不能做什么 / 该换哪个工具」。
    assert "existing UTF-8 text files" in APPLY_PATCH_DESCRIPTION
    assert "cannot create, delete, or move" in APPLY_PATCH_DESCRIPTION
    assert "write_file" in APPLY_PATCH_DESCRIPTION
    # 删除与移动没有专门工具，指向终端。
    assert "execute_terminal" in APPLY_PATCH_DESCRIPTION
    # 参数描述只讲「这个值要填什么格式」。
    assert "unified diff" in patch_description
    assert "'diff --git' header" in patch_description


# 模型可见契约必须自带可用样例与行数规则；2026-09-21 的真实故障正是模型在无样例时
# 只能靠猜（先吐 '*** Begin Patch'，再手写 @@ 计数且算错）。这些内容唯一落在 ``patch`` 参数里：
# 规则在描述正文，最小样例在字段 ``examples``（随 strict schema 导出）。
def test_apply_patch_contract_documents_the_exact_accepted_diff_format() -> None:
    field = ApplyPatchArgs.model_fields["patch"]
    patch_description = field.description or ""
    sample = "\n".join(field.examples or [])

    assert "diff --git a/" in sample
    assert "@@ -" in sample
    assert "*** Begin Patch" in patch_description
    assert "recomputed from the body" in patch_description
    # 工具描述不得回抄任何格式细节（回抄即两处事实源）。
    assert "diff --git" not in APPLY_PATCH_DESCRIPTION
    assert "@@" not in APPLY_PATCH_DESCRIPTION
    assert "*** Begin Patch" not in APPLY_PATCH_DESCRIPTION
    assert "diff --git a/pkg/mod.py" not in APPLY_PATCH_DESCRIPTION


# 行数规则只允许有一处事实源（参数描述）：2026-09-21 的样例计数写错事故正是「同一规则散落在
# 工具描述 / 参数描述 / 解析失败 reason 三处」造成的表述漂移。
def test_patch_count_rule_is_stated_only_in_the_parameter_description() -> None:
    patch_description = ApplyPatchArgs.model_fields["patch"].description or ""

    # 计数契约已改为「按正文重算」（解析器修复不符的计数并回报备注），规则本身仍只在参数描述。
    assert "recomputed from the body" in patch_description
    # 结构性断言：工具描述不得出现计数或 hunk 语法语义，同义改写也拦得住。
    for leaked in (
        "must match",
        "counts",
        "@@ -",
        "context plus",
        "source count",
        "omitted only when",
    ):
        assert leaked not in APPLY_PATCH_DESCRIPTION, leaked


# ``backend_env``（conftest）按生产装配顺序准备主库与工具注册表。
@pytest.mark.usefixtures("backend_env")
def test_handlers_inherit_base_and_new_tools_are_registered() -> None:
    assert all(
        issubclass(handler, HandlerBase) for handler in (ApplyPatchTool,)
    )
    names = set(build_tool_registry().get_all_tool_names())

    assert {"apply_patch", "write_file"} <= names
    assert not {"delete", "delete_file", "move_file"} & names


def test_apply_patch_modifies_existing_file_and_emits_file_changes(tmp_path: Path) -> None:
    target = tmp_path / "existing.txt"
    target.write_text("before\n", encoding="utf-8")

    observation = ApplyPatchTool().execute(_context(tmp_path), patch=_patch())

    assert observation.status == "success"
    assert target.read_text(encoding="utf-8") == "after\n"
    assert observation.display_data["kind"] == "file-changes"
    change = observation.display_data["changes"][0]
    assert change["status"] == "modified"
    assert change["path"] == "existing.txt"


# 回归（2026-09-21 复审发现的误拒）：多文件补丁中间的空行分隔必须照常整体应用。
# Git 实测该形状被忠实应用（`git apply --check` exit 0 且两个文件都按意图改动）。
def test_apply_patch_applies_two_files_separated_by_a_blank_line(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("before\n", encoding="utf-8")
    second.write_text("before\n", encoding="utf-8")
    patch = "\n\n".join((_patch("first.txt"), _patch("second.txt")))

    observation = ApplyPatchTool().execute(_context(tmp_path), patch=patch)

    assert observation.status == "success"
    assert first.read_text(encoding="utf-8") == "after\n"
    assert second.read_text(encoding="utf-8") == "after\n"


def test_apply_patch_rejects_creation_deletion_move_and_workspace_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("before\n", encoding="utf-8")
    target = tmp_path / "existing.txt"
    target.write_text("before\n", encoding="utf-8")
    patches = (
        "\n".join(
            (
                "diff --git a/new.txt b/new.txt",
                "new file mode 100644",
                "--- /dev/null",
                "+++ b/new.txt",
                "@@ -0,0 +1 @@",
                "+new",
            )
        ),
        "\n".join(
            (
                "diff --git a/existing.txt b/existing.txt",
                "deleted file mode 100644",
                "--- a/existing.txt",
                "+++ /dev/null",
                "@@ -1 +0,0 @@",
                "-before",
            )
        ),
        "\n".join(
            (
                "diff --git a/existing.txt b/renamed.txt",
                "similarity index 100%",
                "rename from existing.txt",
                "rename to renamed.txt",
            )
        ),
        _patch("../outside.txt"),
    )

    for patch in patches:
        operations, error = parse_git_unified_diff(patch)
        assert operations == []
        assert error

    assert target.read_text(encoding="utf-8") == "before\n"
    assert outside.read_text(encoding="utf-8") == "before\n"
    assert not (tmp_path / "new.txt").exists()


def test_apply_patch_supports_git_quoted_paths_with_spaces(tmp_path: Path) -> None:
    target = tmp_path / "existing file.txt"
    target.write_text("before\n", encoding="utf-8")
    patch = "\n".join(
        (
            'diff --git "a/existing file.txt" "b/existing file.txt"',
            '--- "a/existing file.txt"',
            '+++ "b/existing file.txt"',
            "@@ -1 +1 @@",
            "-before",
            "+after",
        )
    )

    observation = ApplyPatchTool().execute(_context(tmp_path), patch=patch)

    assert observation.status == "success"
    assert target.read_text(encoding="utf-8") == "after\n"


def test_apply_patch_rejects_legacy_patch_markers_and_prevalidates_all_files(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("before first\n", encoding="utf-8")
    second.write_text("before second\n", encoding="utf-8")
    legacy_marker = _patch() + "\n*** End Patch"
    multi_file_patch = "\n".join(
        (
            "diff --git a/first.txt b/first.txt",
            "--- a/first.txt",
            "+++ b/first.txt",
            "@@ -1 +1 @@",
            "-before first",
            "+after first",
            "diff --git a/second.txt b/second.txt",
            "--- a/second.txt",
            "+++ b/second.txt",
            "@@ -1 +1 @@",
            "-not the current content",
            "+after second",
        )
    )

    marker_operations, marker_error = parse_git_unified_diff(legacy_marker)
    rejected = ApplyPatchTool().execute(_context(tmp_path), patch=multi_file_patch)

    assert marker_operations == []
    assert marker_error
    assert rejected.status == "error"
    assert first.read_text(encoding="utf-8") == "before first\n"
    assert second.read_text(encoding="utf-8") == "before second\n"

