"""Regression guard for hunk line-count diagnostics in ``parse_git_unified_diff``.

背景（2026-09-21 实测缺陷）：一次真实 run 连续 3 次 ``apply_patch`` 失败，被
``tool_error_limit_reached`` 终止整轮。其中第 3 个补丁结构完整，但 6 个 hunk 里有 2 个
``@@`` 头部少算了自身正文行数（声明 ``-112,3 +117,6`` / ``-156,6 +164,8``，实际 4/7 与
7/9）。``unidiff`` 因计数不符提前收尾，随后抛
``Unexpected hunk found: @@ -156,6 +164,8 @@`` —— 该消息既不说明原因、也不指出位置，
模型拿不到任何可执行的修正线索，于是继续失败直到触顶。

本文件冻结**当前**契约（2026-09-21 契约 B）：解析器在**委托 unidiff 之前**逐个 hunk 比对头部
声明计数与正文实际行数；**不再拒绝**，而是**按正文重算计数**（保留起始行号与 ``@@`` 后 section
文本）后照常应用，并把「第几个 file section、第几个 hunk、头部原文、声明值、实际值」一次性作为
**重算提示**写给模型。提示是成功语义下的一部分，不是错误。

计数口径必须与 ``unidiff`` 保持一致，否则会把合法补丁误判为不合法：
空行按 context 计入两侧；省略的计数（``@@ -1 +1 @@``）按 1 计；
``\\ No newline at end of file`` 标记行不计入任何一侧。
"""

from __future__ import annotations

import pytest

from app.core.tools.tool_handler.patch_write.patch_parser import (
    parse_git_unified_diff,
    parse_git_unified_diff_detailed,
)


def _patch(*body: str, path: str = "mod.txt") -> str:
    """拼一个单文件 Git unified diff：头部三行 + 给定正文。"""

    return "\n".join((f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", *body))


# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示。原先「直接拒绝」的期望已过时，本用例改述
# 新契约——计数不符不再阻断应用，但提示必须同时给出头部原文、声明值与实际值（三者缺一模型都
# 无法核对重算结果）；hunk 的 source_length/target_length 必须是**重算后**的值。
def test_mismatched_hunk_is_repaired_with_header_declared_and_actual_counts() -> None:
    patch = _patch("@@ -10,3 +10,6 @@", " keep", "-old", "+new", " tail")

    operations, error = parse_git_unified_diff(patch)
    outcome = parse_git_unified_diff_detailed(patch)

    assert error is None
    assert len(operations) == 1, "计数不符的补丁现在必须被接受"
    assert outcome.count_repairs == [
        "file section 1 hunk 1: '@@ -10,3 +10,6 @@' declared source=3 target=6 but body has "
        "source=3 target=3; counts recomputed from the body and applied as written"
    ]
    assert (operations[0].hunks[0].source_length, operations[0].hunks[0].target_length) == (3, 3)


# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示。一个 section 内多个 hunk 计数不符时必须
# **全部**列出（模型一次就能核对完，不必逐轮试错）；计数自洽的 hunk 不得出现在提示里。
def test_every_mismatched_hunk_is_reported_in_declaration_order() -> None:
    patch = _patch(
        "@@ -1,1 +1,1 @@",
        " keep",
        "-a",
        "+b",
        "@@ -5,3 +5,6 @@",
        " c",
        "-d",
        "@@ -9,2 +9,2 @@",
        " e",
        "-f",
        "+g",
        " h",
    )

    outcome = parse_git_unified_diff_detailed(patch)

    assert outcome.error is None
    assert len(outcome.operations) == 1, "计数不符的补丁现在必须被接受"
    assert len(outcome.count_repairs) == 3, (
        f"三个 hunk 均不符，应各有一条提示：{outcome.count_repairs}"
    )
    repairs = "\n".join(outcome.count_repairs)
    assert "file section 1 hunk 1: '@@ -1,1 +1,1 @@'" in repairs
    assert "file section 1 hunk 2: '@@ -5,3 +5,6 @@'" in repairs
    assert "file section 1 hunk 3: '@@ -9,2 +9,2 @@'" in repairs
    assert repairs.index("hunk 1") < repairs.index("hunk 2") < repairs.index("hunk 3")
    # 重算结果必须写回 hunk 计数：第 1 个 hunk 的正文是 ctx+rem+add（=2/2）。
    first_hunk = outcome.operations[0].hunks[0]
    assert (first_hunk.source_length, first_hunk.target_length) == (2, 2)


# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示。多文件补丁的定位必须带 section 序号，
# 否则模型不知道该核对哪个文件的第几个 hunk 的重算结果。
def test_mismatches_are_reported_per_file_section() -> None:
    patch = "\n".join(
        (
            _patch("@@ -3,2 +3,2 @@", " a", "-b", "+c", " d", path="first.txt"),
            _patch("@@ -4,2 +4,2 @@", " a", "-b", "+c", " d", path="second.txt"),
        )
    )

    outcome = parse_git_unified_diff_detailed(patch)

    assert outcome.error is None
    assert [operation.file_path for operation in outcome.operations] == ["first.txt", "second.txt"]
    repairs = "\n".join(outcome.count_repairs)
    assert "file section 1 hunk 1: '@@ -3,2 +3,2 @@'" in repairs
    assert "file section 2 hunk 1: '@@ -4,2 +4,2 @@'" in repairs



# 反面：计数正确的多 hunk 补丁必须照常解析，新增校验不得扩大拒绝面。
def test_correct_hunk_counts_still_parse() -> None:
    patch = _patch(
        "@@ -1,2 +1,3 @@",
        " a",
        "+b",
        " c",
        "@@ -8,1 +9,1 @@",
        "-d",
        "+e",
    )

    operations, error = parse_git_unified_diff(patch)

    assert error is None
    assert [operation.file_path for operation in operations] == ["mod.txt"]
    assert len(operations[0].hunks) == 2


# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示。省略计数（git 语义为 1）必须按 1 比对——
# 自洽的 ``@@ -1 +1 @@`` 照常接受；少算的 ``@@ -1 +1 @@`` 改为按正文重算并给出提示（原先拒绝）。
def test_omitted_hunk_counts_are_compared_as_one() -> None:
    accepted_operations, accepted_error = parse_git_unified_diff(
        _patch("@@ -1 +1 @@", "-old", "+new")
    )
    repaired_operations, repaired_error = parse_git_unified_diff(
        _patch("@@ -1 +1 @@", "-old", "+new", "+extra")
    )
    repaired_outcome = parse_git_unified_diff_detailed(
        _patch("@@ -1 +1 @@", "-old", "+new", "+extra")
    )

    assert accepted_error is None
    assert [hunk.source_length for hunk in accepted_operations[0].hunks] == [1]
    assert repaired_error is None
    assert [hunk.source_length for hunk in repaired_operations[0].hunks] == [1]
    assert [hunk.target_length for hunk in repaired_operations[0].hunks] == [2]
    assert "declared source=1 target=1" in repaired_outcome.count_repairs[0]
    assert "body has source=1 target=2" in repaired_outcome.count_repairs[0]


# 空正文行在 unidiff 里按 context 计两侧；若不算进计数会产生假告警、错拒合法补丁。
def test_empty_body_line_counts_as_context_for_both_sides() -> None:
    patches = (
        _patch("@@ -1,2 +1,3 @@", " keep", "", "+added"),
        _patch("@@ -1,3 +1,2 @@", " keep", "", "-removed"),
    )

    for patch in patches:
        operations, error = parse_git_unified_diff(patch)

        assert error is None, patch
        assert len(operations[0].hunks) == 1


# 结尾无换行标记既不属于 source 也不属于 target，不能计入任何一侧。
def test_no_newline_marker_is_excluded_from_the_counts() -> None:
    operations, error = parse_git_unified_diff(
        _patch("@@ -1 +1 @@", "-old", "+new", "\\ No newline at end of file")
    )

    assert error is None
    assert operations[0].hunks[0].new_no_newline_at_eof is True


# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示。真实故障形状——第 2 个 hunk 的头部少算正文
# 行数，其多余正文行后面还跟着第 3 个 hunk 头。旧实现下 unidiff 报 `Unexpected hunk found`、Git
# 报 `error: patch fragment without header`（exit 128），两者都不指出真正出错的 hunk；新契约必须
# **只点名第 2 个 hunk** 的计数不符，其余自洽 hunk 不得出现在提示里，且补丁照常被接受。
def test_incident_shape_names_the_under_declared_hunk_before_the_next_header() -> None:
    patch = _patch(
        "@@ -1,2 +1,2 @@",
        " a",
        "-b",
        "+c",
        "@@ -10,2 +10,2 @@",
        " keep",
        "-old",
        "+new",
        " tail",
        "@@ -20,1 +20,1 @@",
        "-x",
        "+y",
    )

    outcome = parse_git_unified_diff_detailed(patch)

    assert outcome.error is None, "计数不符不再阻断应用"
    assert len(outcome.operations) == 1
    assert len(outcome.count_repairs) == 1, f"只有第 2 个 hunk 不符：{outcome.count_repairs}"
    repair = outcome.count_repairs[0]
    assert "file section 1 hunk 2: '@@ -10,2 +10,2 @@'" in repair
    assert "declared source=2 target=2" in repair
    assert "body has source=3 target=3" in repair
    assert "hunk 1" not in repair
    assert "hunk 3" not in repair
    assert "Unexpected hunk found" not in repair


# 回归（2026-09-21 复审发现的误拒）：section 之间的空行是文件段分隔行，不属于前一个 hunk 的
# 正文。Git 实测该形状被**忠实应用**（`git apply --check` exit 0，两个文件都按作者意图改动），
# 把它计入正文会让整份多文件补丁被判「计数不符」而拒绝。
def test_blank_line_between_file_sections_is_not_part_of_the_hunk_body() -> None:
    patch = "\n\n".join(
        (
            _patch("@@ -1,1 +1,1 @@", "-a", "+A", path="f.txt"),
            _patch("@@ -1,1 +1,1 @@", "-x", "+X", path="g.txt"),
        )
    )

    operations, error = parse_git_unified_diff(patch)

    assert error is None
    assert [operation.file_path for operation in operations] == ["f.txt", "g.txt"]


# 回归（同上）：补丁末尾多一个空行属格式噪音，不影响 hunk 正文计数。
def test_trailing_blank_line_at_the_end_of_the_patch_is_ignored() -> None:
    operations, error = parse_git_unified_diff(_patch("@@ -1,1 +1,1 @@", "-a", "+A") + "\n\n")

    assert error is None
    assert [operation.file_path for operation in operations] == ["mod.txt"]


# 回归（同上）：同一 section 内 hunk 之间的空行同样只是分隔，不属于前一个 hunk 的正文。
def test_blank_line_between_hunks_of_one_section_is_not_counted() -> None:
    patch = _patch(
        "@@ -1,1 +1,1 @@",
        "-a",
        "+A",
        "",
        "@@ -9,1 +9,1 @@",
        "-x",
        "+X",
    )

    operations, error = parse_git_unified_diff(patch)

    assert error is None
    assert len(operations[0].hunks) == 2


# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示。真实故障形状——第一个 hunk 的头部少算了正文
# 一行，那个多余行之后还跟着第二个 hunk。Git 对该形状报 `error: patch fragment without header`
# （exit 128），unidiff 报 `Unexpected hunk found`——两者都不指向那一行。新契约必须给出可定位、
# 可核对的重算提示，且**只**点名不符的 hunk 1（自洽的 hunk 2 不得出现），补丁照常被接受。
def test_incident_shape_reports_only_the_under_declared_hunk() -> None:
    patch = _patch(
        "@@ -1,2 +1,2 @@",
        " a",
        "-b",
        "+c",
        " d",
        "@@ -4,1 +4,1 @@",
        "-e",
        "+f",
    )

    outcome = parse_git_unified_diff_detailed(patch)

    assert outcome.error is None, "计数不符不再阻断应用"
    assert len(outcome.operations) == 1
    assert len(outcome.count_repairs) == 1
    repair = outcome.count_repairs[0]
    assert "file section 1 hunk 1: '@@ -1,2 +1,2 @@'" in repair
    assert "declared source=2 target=2" in repair
    assert "body has source=3 target=3" in repair
    assert "hunk 2" not in repair, "计数自洽的第二个 hunk 不应被报出"
    assert "Unexpected hunk found" not in repair


# 2026-09-21 契约 B 新增用例：重算后的 hunk 计数必须等于正文实际行数，且**起始行号保持原样**
# （只改计数、不动 start），section 文本也要保留。
def test_recomputed_counts_equal_the_body_lengths_and_keep_the_start_lines() -> None:
    patch = _patch("@@ -17,2 +25,3 @@ def f():", " a", "-b", "+c", " d")

    operations, error = parse_git_unified_diff(patch)

    assert error is None
    hunk = operations[0].hunks[0]
    assert (hunk.source_start, hunk.target_start) == (17, 25), "起始行号必须保持原样"
    # 正文是 ctx+rem+add+ctx（source=3、target=3），不是头部声明的 2/3。
    assert (hunk.source_length, hunk.target_length) == (3, 3)
    assert hunk.context_hint == "def f():", "``@@`` 后的 section 文本必须保留"


# 2026-09-21 契约 B 新增用例：同一份补丁里只有计数不符的 hunk 出现在 ``count_repairs`` 中，
# 计数自洽的 hunk 不得出现——否则模型无从判断哪些 hunk 被重算过。
def test_only_mismatched_hunks_appear_in_count_repairs() -> None:
    patch = _patch(
        "@@ -1,2 +1,2 @@",
        " a",
        "-b",
        "+c",
        "@@ -9,2 +9,2 @@",
        " e",
        "-f",
        "+g",
        " h",
    )

    outcome = parse_git_unified_diff_detailed(patch)

    assert outcome.error is None
    assert len(outcome.operations[0].hunks) == 2
    assert len(outcome.count_repairs) == 1, f"只有第 2 个 hunk 不符：{outcome.count_repairs}"
    assert "hunk 2: '@@ -9,2 +9,2 @@'" in outcome.count_repairs[0]
    assert "hunk 1" not in outcome.count_repairs[0]
    # 第 1 个 hunk 的计数未被改写：仍等于其正文的 2/2。
    first_hunk = outcome.operations[0].hunks[0]
    assert (first_hunk.source_length, first_hunk.target_length) == (2, 2)


# 2026-09-21 契约 B 新增用例：被**拒绝**的补丁，``count_repairs`` 必须为空列表——重算提示只在
# 接受时有内容，避免模型把「拒绝」误读成「已按正文应用」。
def test_rejected_patch_returns_empty_count_repairs() -> None:
    rejected_shapes = {
        "non-diff text": "not a diff",
        "missing hunks": _patch(),
        "duplicate section": "\n".join(
            (
                _patch("@@ -1,2 +1,2 @@", " a", "-b", "+c"),
                _patch("@@ -4,2 +4,2 @@", " d", "-e", "+f"),
            )
        ),
        "context-only hunk": _patch("@@ -1,1 +1,1 @@", " only"),
        "under-declared but no real change": _patch("@@ -1,2 +1,2 @@", " a", " b", " c"),
    }

    for name, patch in rejected_shapes.items():
        outcome = parse_git_unified_diff_detailed(patch)

        assert outcome.operations == [], name
        assert outcome.error is not None, name
        assert outcome.count_repairs == [], f"{name}: 被拒绝时 count_repairs 必须为空"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
