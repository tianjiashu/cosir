"""Adversarial tests for the hunk line-count recomputation pass.

被测对象：``patch_parser._recompute_hunk_counts`` 及其在 ``parse_git_unified_diff`` /
``parse_git_unified_diff_detailed`` / ``ApplyPatchTool`` / ``FileResourceResolver`` 中的接线。
目标是**发现缺陷**，不是复述实现。

2026-09-21 契约 B：计数不符的补丁**不再被拒绝**，而是按正文重算 ``@@`` 头后照常应用，并把
重算明细（section/hunk 序号、头部原文、声明值、实际值）作为**提示**交给模型。因此本文件的
攻击面随之调整：

1. 误拒（最致命）：任何口径偏差都会把*自洽*补丁拒掉，或让自洽 hunk 被误报进提示。判定基准是
   「头部声明的行数 == 作者写下的正文行数」（``_body_exactly_matches_declared``），**不是**
   ``unidiff`` 的宽松消费语义。计数重算的计数口径必须与 ``unidiff`` 一致，否则会把合法补丁
   误判为需要重算。
2. 漏报/静默：计数不符的 hunk **必须**出现在 ``count_repairs`` 里，且重算后的
   ``source_length`` / ``target_length`` 必须等于正文实际行数；自洽的 hunk 不得出现。
3. 诊断质量：section/hunk 序号、头部原文、声明值、实际值齐全且顺序稳定；纯 ASCII。
4. 安全性：接受（重算）后**必须真的写文件**；被拒绝的补丁仍绝不写文件、``retryable`` 语义
   不变、不得为无效补丁申请资源锁；接受后 ``FileResourceResolver`` **必须**申请写路径与锁。
5. 幂等 / 无副作用：可重复调用结果一致、不修改入参；``section_index`` 呈现从 1 起。
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest
from unidiff import PatchSet
from unidiff.constants import RE_HUNK_HEADER
from unidiff.errors import UnidiffParseError

from app.core.tools.guard.file_resource_paths import FileResourceResolver
from app.core.tools.schemas import ToolExecutionContext
from app.core.tools.tool_handler.apply_patch_tool import ApplyPatchTool
from app.core.tools.tool_handler.patch_write.patch_parser import (
    parse_git_unified_diff,
    parse_git_unified_diff_detailed,
)

# 2026-09-21 契约 B：计数不符不再产生拒绝诊断。把旧拒绝文案固化为常量，任何位置一旦重新出现
# 即视为「回退到拒绝语义」的回归信号（原有「不得误拒」的守卫由此改为显式的反拒绝断言）。
LEGACY_COUNT_REJECTION = "hunk line counts do not match"

# ``_recompute_hunk_counts`` 是模块私有函数：本文件用「报告型契约」测试它的直接行为，
# 同时所有缺陷都以 ``parse_git_unified_diff_detailed`` 的公开返回值为准，避免只测私有实现。
def _hunk_count_diagnostics(section: str, section_index: int) -> list[str]:
    """测试侧视图：解析层现在「按正文重算计数」，本函数返回重算时产出的提示列表。"""

    module = pytest.importorskip("app.core.tools.tool_handler.patch_write.patch_parser")
    return module._recompute_hunk_counts(section, section_index)[1]


def _body_exactly_matches_declared(header: str, body: list[str]) -> bool:
    """该 (头部, 正文) 是否**本身就自洽**——即头部声明的行数恰好等于正文实际行数。

    用于在穷举与「漏放」用例里先证明「这个输入本身是对的」，避免把夹具自身的问题
    误报成生产缺陷（测试自身问题必须与生产缺陷严格区分）。
    """

    match = RE_HUNK_HEADER.match(header)
    assert match is not None, f"fixture header is not a valid hunk header: {header!r}"
    declared_source = int(match.group(2) or 1)
    declared_target = int(match.group(4) or 1)
    source = sum(1 for line in body if line[:1] in {" ", "", "-"})
    target = sum(1 for line in body if line[:1] in {" ", "", "+"})
    return (declared_source, declared_target) == (source, target)


def _section(header: str, *body: str, path: str = "mod.txt") -> str:
    """拼一个单文件 Git unified diff：头部三行 + 给定 ``@@`` 头 + 正文。"""

    return "\n".join(
        (f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", header, *body)
    )


def _serialized_body(header: str, body: list[str]) -> list[str]:
    """返回该 (头部, 正文) 经 ``_section`` 序列化后**解析器真正看到**的正文行。

    ``"\\n".join`` 会让「末尾的空元素」只贡献一个换行符、并不构成一行：``[" a", ""]`` 被序列化成
    ``" a\\n"``，解析器与 Git 都只看到 1 行。若直接拿入参列表计数，会把这种输入误判成「头部与正文
    自洽」，进而把生产侧的正确拒绝误报为缺陷。
    """

    return _section(header, *body).splitlines()[4:]


def _unidiff_verdict(patch: str) -> tuple[bool, str]:
    """按生产实际投喂方式询问语义权威 unidiff 是否接受该补丁。

    复刻 ``parse_git_unified_diff`` 的路径规范化（``a/mod.txt b/mod.txt`` →
    ``a/codex_patch_file_0 b/codex_patch_file_0``），再交给 ``unidiff``：

    返回:
        ``(True, "ok")`` 表示 unidiff 接受且每个 hunk 的声明计数与它解析出的正文一致；
        ``(False, 原因)`` 表示 unidiff 拒绝或解析结果自相矛盾。
    """

    normalized = patch
    if "a/mod.txt b/mod.txt" in normalized:
        normalized = normalized.replace(
            "a/mod.txt b/mod.txt",
            "a/codex_patch_file_0 b/codex_patch_file_0",
        ).replace("--- a/mod.txt", "--- a/codex_patch_file_0").replace(
            "+++ b/mod.txt", "+++ b/codex_patch_file_0"
        )
    try:
        parsed = PatchSet.from_string(normalized)
    except (UnidiffParseError, ValueError, IndexError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    for patched_file in parsed:
        for hunk in patched_file:
            declared = (hunk.source_length, hunk.target_length)
            actual = (
                sum(1 for line in hunk if line.line_type in " -"),
                sum(1 for line in hunk if line.line_type in " +"),
            )
            if declared != actual:
                return False, f"unidiff kept declared={declared} but parsed body={actual}"
    return True, "ok"


def _legacy_count_rejection(error: str | None) -> bool:
    """该 error 是否仍是「计数不符 → 拒绝」的旧契约文案。

    2026-09-21 契约 B 之后解析层不再用计数不符拒绝补丁，故本函数在正常路径应恒为 ``False``；
    用例用它把「拒绝面被悄悄放宽」或「拒绝语义回退」都变成可观测的失败。
    """

    return bool(error) and LEGACY_COUNT_REJECTION in str(error)


def _context(root: Path) -> ToolExecutionContext:
    return ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=root, run_id=4242)


# --------------------------------------------------------------------------------------
# 攻击面 1：误拒（最致命）
# --------------------------------------------------------------------------------------


# 目的：unidiff 真接受、计数自洽的补丁不得被本校验拒。
# 缺陷类型：口径偏差导致误拒（合法补丁被判非法）。
def test_self_consistent_patches_are_never_rejected_by_the_count_guard() -> None:
    patches = {
        "plain context/removed/added": _section("@@ -1,2 +1,2 @@", " a", "-b", "+c"),
        "tab-indented context body": _section("@@ -1,2 +1,2 @@", " \tkeep", "-old", "+new"),
        "trailing spaces preserved": _section("@@ -1,2 +1,2 @@", " a  ", "-b  ", "+c  "),
        "section text after @@": _section("@@ -1,2 +1,2 @@ def f():", " a", "-b", "+c"),
        "omitted counts as one": _section("@@ -1 +1 @@", "-old", "+new"),
        "zero-count deletion side": _section("@@ -1,1 +0,0 @@", "-removed"),
        "pure insertion": _section("@@ -0,0 +1,2 @@", "+first", "+second"),
        "explicit zero source count": _section("@@ -1,0 +1,1 @@", "+added"),
        "no-newline marker mid-hunk": _section(
            "@@ -1,1 +1,1 @@", "-a", "\\ No newline at end of file", "+b"
        ),
        "no-newline marker at end": _section(
            "@@ -1,1 +1,1 @@", "-a", "+b", "\\ No newline at end of file"
        ),
        "two no-newline markers": _section(
            "@@ -1,1 +1,1 @@",
            "-a",
            "\\ No newline at end of file",
            "+b",
            "\\ No newline at end of file",
        ),
        "empty body line as context": _section("@@ -1,2 +1,3 @@", " a", "", "+c"),
        "two empty body lines": _section("@@ -1,3 +1,4 @@", " a", "", "", "+c"),
        "empty body line then addition": _section("@@ -1,1 +1,2 @@", "", "+c"),
        "content line starting with @": _section(
            "@@ -1,2 +1,2 @@", " a", "-@decorator", "+@property"
        ),
        "content line looking like a hunk header": _section(
            "@@ -1,2 +1,2 @@", " a", "-@@ -1,1 +1,1 @@", "+@@ -2,2 +2,2 @@"
        ),
        "incrementing prefix content": _section("@@ -1,2 +1,2 @@", " a", "-++i", "+i++"),
        "dashed separator content": _section("@@ -1,2 +1,2 @@", " a", "- ---", "+----"),
        "long single line": _section("@@ -1 +1 @@", "-" + "x" * 5000, "+" + "y" * 5000),
    }

    for name, patch in patches.items():
        operations, error = parse_git_unified_diff(patch)
        accepted_by_unidiff, detail = _unidiff_verdict(patch)

        assert accepted_by_unidiff, f"{name}: unidiff rejected the fixture itself: {detail}"
        assert not _legacy_count_rejection(error), f"误拒：{name} 被计数校验拒绝，error={error!r}"
        assert error is None, f"{name}: expected acceptance, got error={error!r}"
        assert operations, f"{name}: expected parsed operations"
        # 自洽补丁不得产生任何重算提示（否则就是把自洽 hunk 误报进 count_repairs）。
        assert parse_git_unified_diff_detailed(patch).count_repairs == [], name


# 目的：CRLF 与裸 CR 正文的计数口径必须与 unidiff 一致。缺陷类型：按 splitlines 分段导致的行数错算。
@pytest.mark.parametrize(
    "name",
    ["crlf_body", "crlf_whole_patch", "cr_only_body_line", "mixed_crlf_and_lf_body"],
)
def test_carriage_return_variants_follow_the_authoritative_parser(name: str) -> None:
    lf = _section("@@ -1,2 +1,2 @@", " line one", "-old line", "+new line")
    patches = {
        "crlf_body": _section("@@ -1,2 +1,2 @@", " line one\r", "-old line\r", "+new line\r"),
        "crlf_whole_patch": lf.replace("\n", "\r\n"),
        "cr_only_body_line": _section("@@ -1,2 +1,2 @@", " line one\r", "-old line", "+new line"),
        "mixed_crlf_and_lf_body": _section(
            "@@ -1,2 +1,2 @@", " line one\r", "-old line\n", "+new line\r"
        ),
    }
    patch = patches[name]

    operations, error = parse_git_unified_diff(patch)
    accepted_by_unidiff, detail = _unidiff_verdict(patch)

    if accepted_by_unidiff:
        assert not _legacy_count_rejection(error), (
            f"误拒：{name} unidiff 接受（{detail}）但计数校验拒绝：{error!r}"
        )
        assert error is None, f"{name}: unidiff 接受但解析器拒绝：{error!r}"
    else:
        # unidiff 的判定只用于漏放方向的反向校验：它拒绝时，解析器绝不能静默放行为
        # 「计数自洽的正常补丁」。契约 B 下计数不符会被重算，因此允许两种结果之一：
        # 要么被解析器拒绝，要么被重算（count_repairs 非空）后接受。
        accepted_after_repair = error is None and parse_git_unified_diff_detailed(
            patch
        ).count_repairs
        assert error is not None or accepted_after_repair, (
            f"{name}: unidiff 拒绝且解析器既未拒绝也无重算提示（漏放）：{detail}"
        )


# 目的：正文里含以 @/+/- 开头的**内容**时（Python 装饰器、``++i``、形如 hunk 头的字符串字面量），
# 前缀必须按 diff 语义计数、不得被当成新 hunk 的边界。
# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示。原夹具把 target 声明为 3，但正文
# ctx+rem(装饰器)+add(装饰器)+rem 的 target 实为 2——旧契约下这属「头部与正文不符 → 拒绝」，
# 新契约下改为重算并给出提示；同时锁死「伪头部不得被当成边界」这一原始意图不变。
def test_prefixed_header_looking_content_counts_by_prefix_not_as_boundary() -> None:
    body = (
        " def f():",
        "-    @wraps(inner)",
        "+    @functools.wraps(inner)",
        "-@@ -1,1 +1,1 @@",
    )
    consistent = _section("@@ -1,3 +1,2 @@", *body)
    under_declared = _section("@@ -1,3 +1,3 @@", *body)

    operations, error = parse_git_unified_diff(consistent)
    under_declared_outcome = parse_git_unified_diff_detailed(under_declared)

    assert error is None, f"含伪头部正文的自洽补丁被拒绝：{error!r}"
    assert len(operations[0].hunks) == 1, "带前缀的伪头部正文行不得被当成新 hunk 的边界"
    assert [line.content for line in operations[0].hunks[0].lines if line.prefix == "-"] == [
        "    @wraps(inner)",
        "@@ -1,1 +1,1 @@",
    ], "伪头部内容必须作为被删除行逐字保留"
    # 重算依据必须是计数，而不是把伪头部当成边界：同正文配 3/3 头部时命中 source 相符、target 不符。
    assert under_declared_outcome.error is None
    assert len(under_declared_outcome.operations[0].hunks) == 1
    assert len(under_declared_outcome.count_repairs) == 1
    assert "body has source=3 target=2" in under_declared_outcome.count_repairs[0]


# 目的：``@@`` 头声明为 0 的边界（``@@ -0,0 +1,0 @@`` 与 ``@@ -1,0 +1,0 @@``）不得因 0 被当成省略。
# 缺陷类型：`or 1` 把显式 0 改写成 1。
def test_explicit_zero_counts_are_not_coerced_to_one() -> None:
    # ``@@ -1,0 +1,1 @@``：source 显式 0、target 1 —— 只有一行新增。
    header = RE_HUNK_HEADER.match("@@ -1,0 +1,1 @@")
    assert header is not None and header.group(2) == "0", "前置条件：unidiff 正则能捕获显式 0"

    patch_with_body = _section("@@ -1,0 +1,1 @@", "+added")
    operations, error = parse_git_unified_diff(patch_with_body)

    assert error is None, f"显式 0 计数被误判：{error!r}"
    assert operations[0].hunks[0].source_length == 0
    # 显式 0 若被 `or 1` 当成省略写回 1，下一步 unidiff 会报 "Hunk is longer than expected"。
    assert _unidiff_verdict(_section("@@ -1,0 +1,1 @@", "+added"))[0]

    # 显式 0 若被 `or 1` 当成省略写回 1，上面已被 unidiff 自我校验覆盖；此处补充
    # 「显式 0 计数与省略计数不可互换」的直接契约：正则必须能把两者区分开。
    asserted = RE_HUNK_HEADER.match("@@ -1,0 +1,1 @@")
    omitted = RE_HUNK_HEADER.match("@@ -1 +1 @@")
    assert asserted is not None and omitted is not None
    assert asserted.group(2) == "0", "显式 0 必须能被正则捕获"
    assert omitted.group(2) is None, "省略计数的 group 必须为 None（与显式 0 区分）"


# 目的：多文件多 hunk 混合、以及同一 section 内多个 hunk 的边界必须与 unidiff 一致。缺陷类型：跨
# section 的 hunk 切片错位。
def test_multi_file_multi_hunk_patch_counts_match_the_authoritative_parser() -> None:
    patch = "\n".join(
        (
            _section("@@ -1,2 +1,2 @@", " a", "-b", "+c", path="first.txt"),
            _section("@@ -5,3 +5,3 @@ def g():", " d", "-e", "+f", " g", path="second.txt"),
            _section("@@ -9 +9 @@", "-h", "+i", path="third.txt"),
        )
    )

    operations, error = parse_git_unified_diff(patch)
    accepted_by_unidiff, detail = _unidiff_verdict(patch)

    assert accepted_by_unidiff, f"fixture itself rejected by unidiff: {detail}"
    assert error is None, f"多文件合法补丁被拒绝：{error!r}"
    assert [operation.file_path for operation in operations] == [
        "first.txt",
        "second.txt",
        "third.txt",
    ]
    assert [len(operation.hunks) for operation in operations] == [1, 1, 1]


# 目的：BOM 前缀属于 patch 层问题而非计数问题；但若被判为计数问题会给出误导性诊断。
# 缺陷类型：诊断归因错误。
# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示。BOM 补丁在更早的 header 阶段就被拒绝，
# 因此判定改为「被拒绝且 count_repairs 为空」（BOM 不得被归因为任何 hunk 的计数不符）。
def test_bom_prefixed_patch_is_not_reported_as_a_count_mismatch() -> None:
    patch = "\ufeff" + _section("@@ -1 +1 @@", "-old", "+new")

    operations, error = parse_git_unified_diff(patch)
    outcome = parse_git_unified_diff_detailed(patch)

    assert operations == []
    assert error is not None
    assert not _legacy_count_rejection(error), f"BOM 不应被归因为计数不符：{error!r}"
    assert outcome.count_repairs == [], f"BOM 不应产出任何重算提示：{outcome.count_repairs!r}"


# --- 缺陷 1（误拒）：正文里出现形如 ''@@ -x,y +a,b @@'' 的**内容**行，hunk 边界被误判 ---


# 目的：内容行恰好符合 RE_HUNK_HEADER（如 ``-@@ -1,1 +1,1 @@`` 或 ``+@@ -2,2 +2,2 @@``）时，
# 它仍是**正文行**而非新 hunk 头。缺陷类型：私有扫描器只按 ``line.startswith("@@ ")`` 切分
# 又先用 RE_HUNK_HEADER 判定，导致把被 -/+ 前缀保护的内容误认为 hunk 边界，从而漏算尾部正文。
def test_fake_hunk_header_inside_body_is_not_treated_as_a_boundary() -> None:
    # 头部用自洽的 3/3（正文 ctx+rem+add+ctx）。原夹具声明 2/2 与正文不符，其拒绝依据是
    # 计数而非「边界误判」，因此这里必须换成自洽头部，才能单独检验边界语义。
    patch = _section(
        "@@ -1,3 +1,3 @@",
        " a",
        "-@@ -1,1 +1,1 @@",
        "+b",
        " c",
    )

    operations, error = parse_git_unified_diff(patch)

    assert error is None, f"含伪头部正文的自洽补丁被拒绝：{error!r}"
    assert len(operations[0].hunks) == 1, "带 '-' 前缀的伪头部不得被当成 hunk 边界"
    assert [line.content for line in operations[0].hunks[0].lines if line.prefix == "-"] == [
        "@@ -1,1 +1,1 @@"
    ], "伪头部内容必须作为被删除行逐字保留"


# 目的：只有**新增**行内容像 hunk 头时的最小复现。缺陷类型：同上，且更易触发（一行即误拒）。
def test_added_line_looking_like_a_hunk_header_does_not_break_counts() -> None:
    # 正文为 ctx+add+rem+ctx，自洽头部是 3/3（原夹具声明 2/3 与正文不符）。
    patch = _section("@@ -1,3 +1,3 @@", " a", "+@@ -5,5 +5,5 @@", "-c", " d")

    operations, error = parse_git_unified_diff(patch)

    assert error is None, f"含伪头部新增行的自洽补丁被拒绝：{error!r}"
    assert len(operations[0].hunks) == 1, "带 '+' 前缀的伪头部不得被当成 hunk 边界"
    assert [line.content for line in operations[0].hunks[0].lines if line.prefix == "+"] == [
        "@@ -5,5 +5,5 @@"
    ], "伪头部内容必须作为新增行逐字保留"


# --- 缺陷 2（误拒）：空正文行按 context 计，但未计入头部声明的尾部正文被漏算 ---


# 目的：空正文行（``RE_HUNK_EMPTY_BODY_LINE``）在 unidiff 里按 context 计两侧；当补齐计数后，
# 其**后**的正文行仍属于同一 hunk。缺陷类型：把满足计数的空行当作 hunk 结束，漏算其后正文。
def test_empty_body_line_does_not_terminate_the_hunk_early() -> None:
    # 空正文行按 context 计入两侧，因此 ctx+空行+rem+add 的自洽头部是 3/3。
    patch = _section("@@ -1,3 +1,3 @@", " a", "", "-b", "+c")

    operations, error = parse_git_unified_diff(patch)

    assert error is None, f"含空正文行的自洽补丁被拒绝：{error!r}"
    assert len(operations[0].hunks) == 1, "空正文行不得终止 hunk"
    assert [(line.prefix, line.content) for line in operations[0].hunks[0].lines] == [
        (" ", "a"),
        (" ", ""),
        ("-", "b"),
        ("+", "c"),
    ], "空行必须按 context 保留为独立正文行"


# 目的：把「头部计数小于作者写下的正文」这一根因做成最小复现（无需空行/伪头部）。
# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示。该形状现在**被接受**（按正文的 3/3 应用），
# 并把「声明 2/2、实际 3/3」这样可直接核对的数字写进提示；重算后的 hunk 计数必须等于正文实际
# 行数。原「必须在应用前拒绝」的期望已过时。
def test_under_declared_hunk_body_is_repaired_with_correctable_numbers() -> None:
    patch = _section("@@ -1,2 +1,2 @@", " a", "-b", "+c", " d")

    outcome = parse_git_unified_diff_detailed(patch)

    assert outcome.error is None, "计数不符不再阻断应用"
    assert len(outcome.operations) == 1
    assert len(outcome.count_repairs) == 1
    repair = outcome.count_repairs[0]
    assert "declared source=2 target=2" in repair
    assert "body has source=3 target=3" in repair
    hunk = outcome.operations[0].hunks[0]
    assert (hunk.source_length, hunk.target_length) == (3, 3)


# --- 缺陷 3（误拒）：显式 0 与省略计数混用 ---


# 目的：``@@ -1,0 +1,1 @@`` 中 source 显式声明 0；正文首行之后的内容不应再计入本 hunk。
# 缺陷类型：显式 0 与「省略=1」口径混用，把已完成的 hunk 的后续内容错算进 body。
# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示。正文是 add + ctx（source=1、target=2），
# 与头部声明的 0/1 不符——现在按正文重算为 1/2 并接受（作者写下的 ' keep' 不再被静默丢弃），
# 提示里给出声明值与实际值供核对。原「必须按计数拒绝」的期望已过时。
def test_explicit_zero_source_hunk_is_recomputed_from_the_following_body() -> None:
    patch = _section("@@ -1,0 +1,1 @@", "+added", " keep")

    outcome = parse_git_unified_diff_detailed(patch)

    assert outcome.error is None, "计数不符不再阻断应用"
    assert len(outcome.operations) == 1
    assert len(outcome.count_repairs) == 1
    repair = outcome.count_repairs[0]
    assert "declared source=0 target=1" in repair
    assert "body has source=1 target=2" in repair
    hunk = outcome.operations[0].hunks[0]
    assert (hunk.source_length, hunk.target_length) == (1, 2)
    assert [(line.prefix, line.content) for line in hunk.lines] == [("+", "added"), (" ", "keep")]


# --------------------------------------------------------------------------------------
# 攻击面 2：漏报（计数不符被静默放行，或重算值不正确）
# --------------------------------------------------------------------------------------


# 目的：头部计数**少于**正文行数的补丁不得被静默放行——契约 B 下必须出现在 ``count_repairs`` 里，
# 且重算后的计数必须等于正文实际行数。缺陷类型：口径不一致造成的漏报 / 重算值错误。
# 说明：这里的夹具都是「正文行数多于声明」——unidiff 会提前收尾，声明与解析出的正文必然不等，
# 故属真正的计数不符。
def test_under_counting_header_is_always_repaired_and_never_silently_accepted() -> None:
    miscounted = {
        "source and target both under-counted": (
            "@@ -1,2 +1,2 @@",
            [" a", "-b", "+c", " d"],
            (3, 3),
        ),
        "only target under-counted": ("@@ -1,1 +1,2 @@", [" a", "-b", "+c"], (2, 2)),
        "only source under-counted": ("@@ -1,2 +1,1 @@", [" a", "-b", "+c"], (2, 2)),
        "zero target with added body": ("@@ -1,1 +0,0 @@", ["-a", "+b"], (1, 1)),
    }

    for name, (header, body, expected) in miscounted.items():
        assert not _body_exactly_matches_declared(
            header, _serialized_body(header, body)
        ), f"{name}: 夹具自身其实自洽"
        patch = _section(header, *body)
        outcome = parse_git_unified_diff_detailed(patch)

        assert outcome.error is None, f"{name}: 契约 B 下计数不符应被接受（按正文重算）"
        assert len(outcome.count_repairs) == 1, (
            f"漏报：{name} 计数不符却没有任何重算提示：{outcome.count_repairs}"
        )
        hunk = outcome.operations[0].hunks[0]
        assert (hunk.source_length, hunk.target_length) == expected, (
            f"{name}: 重算后的计数必须等于正文实际行数"
        )
        assert not _legacy_count_rejection(outcome.error)


# 目的：计数**多于**正文行数（正文提前结束）也必须被重算并给出提示，而不是交给 unidiff 抛出
# 无定位的异常，也不是静默接受。缺陷类型：漏报 / 重算值错误。
def test_over_counting_header_is_repaired_and_reported() -> None:
    header = "@@ -1,6 +1,6 @@"
    body = [" a", "-b", "+c", " d", "-e", "+f"]

    assert not _body_exactly_matches_declared(header, _serialized_body(header, body)), (
        "夹具自身其实自洽"
    )
    patch = _section(header, *body)
    outcome = parse_git_unified_diff_detailed(patch)

    assert outcome.error is None, "计数不符不再阻断应用"
    assert len(outcome.count_repairs) == 1, (
        "漏报：声明多于正文（unidiff 会报 'Hunk is shorter than expected'）却无重算提示："
        f"{outcome.count_repairs!r}"
    )
    repair = outcome.count_repairs[0]
    assert "declared source=6 target=6" in repair
    assert "body has source=4 target=4" in repair
    hunk = outcome.operations[0].hunks[0]
    assert (hunk.source_length, hunk.target_length) == (4, 4)


# 目的：计数不符的**首个** hunk 必须被点名，且提示完整可核对（含头部原文/声明/实际）。
# 缺陷类型：只报末位 hunk 或丢失上下文。
def test_first_mismatched_hunk_is_reported_with_actionable_detail() -> None:
    patch = _section(
        "@@ -10,2 +10,2 @@",
        " a",
        "-b",
        "+c",
        " d",
    )

    outcome = parse_git_unified_diff_detailed(patch)

    assert outcome.error is None
    assert len(outcome.count_repairs) == 1
    repair = outcome.count_repairs[0]
    assert "file section 1 hunk 1:" in repair
    assert "'@@ -10,2 +10,2 @@'" in repair
    assert "declared source=2 target=2" in repair
    assert "body has source=3 target=3" in repair


# --------------------------------------------------------------------------------------
# 攻击面 3：诊断质量
# --------------------------------------------------------------------------------------


# 目的：多 section 多 hunk 的重算提示必须全部列出、顺序稳定、定位精确。
# 缺陷类型：聚合丢项/顺序不稳定/序号从 0 起。
# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示。夹具里的「自洽」section 必须是**真正会改动
# 内容**的 hunk（纯 context 的 hunk 会被 'hunk does not change file contents' 拒绝，那是另一条
# 契约，与本用例无关）；四个 section 用不同路径，避免被 duplicate-section 规则截断。
def test_all_mismatches_are_listed_in_stable_section_and_hunk_order() -> None:
    patch = "\n".join(
        (
            _section("@@ -1,2 +1,2 @@", " ok", "-old", "+new", path="first.txt"),
            _section("@@ -2,2 +2,3 @@", " a", "-b", "+c", " d", path="second.txt"),
            _section("@@ -5,5 +5,5 @@", " d", "-e", "+f", " g", path="third.txt"),
            _section("@@ -9,2 +9,2 @@", " g", "-old", "+new", path="fourth.txt"),
        )
    )

    first_outcome = parse_git_unified_diff_detailed(patch)
    second_outcome = parse_git_unified_diff_detailed(patch)

    assert first_outcome.error is None
    assert len(first_outcome.operations) == 4, "四个 section 均应被接受"
    assert first_outcome.count_repairs == second_outcome.count_repairs, (
        "同一输入两次调用的提示必须完全一致（顺序稳定）"
    )

    repairs = "\n".join(first_outcome.count_repairs)
    assert "file section 2 hunk 1: '@@ -2,2 +2,3 @@'" in repairs
    assert "file section 3 hunk 1: '@@ -5,5 +5,5 @@'" in repairs
    assert "file section 1 hunk 1" not in repairs, "自洽的 hunk 不得出现在提示中"
    assert "file section 4" not in repairs, "自洽的 section 不得出现在提示中"
    assert repairs.index("section 2") < repairs.index("section 3")


# 目的：重算提示必须是纯 ASCII，GBK 控制台 ``print`` 不得崩。缺陷类型：混入非 ASCII
# 引号/破折号导致编码异常。
def test_diagnostics_are_pure_ascii_and_printable(capsys) -> None:
    patch = _section("@@ -1,2 +1,2 @@", " a", "-b", "+c", " d")

    outcome = parse_git_unified_diff_detailed(patch)

    assert len(outcome.count_repairs) == 1
    repair = outcome.count_repairs[0]
    repair.encode("ascii")  # 非 ASCII 会在此抛 UnicodeEncodeError
    print(repair)
    captured = capsys.readouterr()
    assert "counts recomputed from the body and applied as written" in captured.out


# 目的：``_recompute_hunk_counts`` 的 section_index 呈现必须从 1 起（模型可读性契约）。
# 缺陷类型：off-by-one 导致定位错位。
def test_diagnostics_report_one_based_section_index() -> None:
    section = _section("@@ -1,2 +1,2 @@", " a", "-b", "+c", " d")

    diagnostics_at_zero = _hunk_count_diagnostics(section, 0)
    diagnostics_at_four = _hunk_count_diagnostics(section, 4)

    assert len(diagnostics_at_zero) == 1
    assert diagnostics_at_zero[0].startswith("file section 1 hunk 1:")
    assert diagnostics_at_four[0].startswith("file section 5 hunk 1:")


# 目的：hunk 序号必须按 section 内出现顺序从 1 递增，即使多个 hunk 都要重算。
# 缺陷类型：序号复用/跳跃。
def test_diagnostics_number_hunks_sequentially() -> None:
    section = _section(
        "@@ -1,2 +1,2 @@",
        " a",
        "-b",
        "+c",
        " d",
        "@@ -9,2 +9,2 @@",
        " e",
        "-f",
        "+g",
        " h",
    )

    diagnostics = _hunk_count_diagnostics(section, 0)

    assert len(diagnostics) == 2
    assert diagnostics[0].startswith("file section 1 hunk 1:")
    assert diagnostics[1].startswith("file section 1 hunk 2:")


# --------------------------------------------------------------------------------------
# 攻击面 4：安全性
# --------------------------------------------------------------------------------------


# 目的：计数不符不再阻断应用——工具必须报 ``success``、给出完整重算提示，且**确实写入**文件
# （按正文重算后的结果）。
# 缺陷类型：接受后却没写文件（提示与实际不一致）。
# 2026-09-21 契约 B：原「拒绝 + 零写入」的期望已过时，翻转为「接受 + 确实写入 + 提示完整」。
def test_miscounted_patch_is_applied_with_a_complete_note(tmp_path: Path) -> None:
    target = tmp_path / "mod.txt"
    target.write_text("a\nb\n", encoding="utf-8")
    patch = _section("@@ -1,2 +1,2 @@", " a", "-b", "+c", " d")

    observation = ApplyPatchTool().execute(_context(tmp_path), patch=patch)

    assert observation.status == "success"
    assert observation.error is None
    assert observation.content is not None
    assert observation.content.startswith("success\n")
    assert "'@@ -1,2 +1,2 @@' declared source=2 target=2 but body has source=3 target=3" in (
        observation.content
    )
    # 正文是 ctx+rem+add+ctx，重算为 3/3；文件确实被写入为 'a\nc\nd\n'。
    assert target.read_text(encoding="utf-8") == "a\nc\nd\n"


# 目的：多文件补丁中仅一个 section 计数不符时，两个文件仍都必须被写入（契约 B 下计数不符不再
# 阻断应用），且重算提示只点名不符的那个 section。
# 缺陷类型：接受却未逐文件写入 / 提示张冠李戴。
def test_one_miscounted_section_still_writes_every_file(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("a\nb\n", encoding="utf-8")
    second.write_text("c\nd\n", encoding="utf-8")
    patch = "\n".join(
        (
            _section("@@ -1,2 +1,2 @@", " a", "-b", "+c", path="first.txt"),
            _section("@@ -1,2 +1,2 @@", " c", "-d", "+e", " f", path="second.txt"),
        )
    )

    observation = ApplyPatchTool().execute(_context(tmp_path), patch=patch)

    assert observation.status == "success"
    assert first.read_text(encoding="utf-8") == "a\nc\n"
    assert second.read_text(encoding="utf-8") == "c\ne\nf\n"
    assert observation.content is not None
    assert "file section 2 hunk 1: '@@ -1,2 +1,2 @@'" in observation.content
    assert "file section 1 hunk 1" not in observation.content, "自洽的 section 不得出现在提示中"


# 目的：计数不符的补丁现在**会**被工具接受并写入，因此资源解析**必须**为它申请写路径与锁
# （否则调度层与实际写入不一致，可能并发覆盖同一文件）。
# 缺陷类型：工具实际写入但资源解析未申请锁（锁语义与实际写入失配）。
# 2026-09-21 契约 B：原「计数不符 → 不申请锁」的期望已过时，翻转为「确实申请锁」。
def test_miscounted_patch_requests_its_resource_lock(tmp_path: Path) -> None:
    patch = _section("@@ -1,2 +1,2 @@", " a", "-b", "+c", " d")

    resources = FileResourceResolver(tmp_path).resolve("apply_patch", {"patch": patch})

    assert resources.write_paths == (tmp_path / "mod.txt",)
    assert (tmp_path / "mod.txt") in resources.lock_paths


# 目的：真正**被拒绝**的补丁（非计数不符形状）仍不得申请任何资源锁。
# 缺陷类型：锁申请先于校验。
def test_invalid_patch_requests_no_resource_lock(tmp_path: Path) -> None:
    patch = "not a diff"

    resources = FileResourceResolver(tmp_path).resolve("apply_patch", {"patch": patch})

    assert resources.write_paths == ()
    assert resources.lock_paths == ()


# 目的：计数自洽的补丁必须照常申请写路径与锁（新增校验不得破坏调度链）。
# 缺陷类型：过度拒绝导致资源未解析。
def test_valid_patch_still_requests_write_paths_and_locks(tmp_path: Path) -> None:
    patch = _section("@@ -1,2 +1,2 @@", " a", "-b", "+c")

    resources = FileResourceResolver(tmp_path).resolve("apply_patch", {"patch": patch})

    assert resources.write_paths == (tmp_path / "mod.txt",)
    assert (tmp_path / "mod.txt") in resources.lock_paths
    assert resources.write_paths, "合法补丁必须解析出写路径，否则会与 handler 的校验阶段失配"


# 目的：计数不符时模型可见通道必须给出**完整重算提示**（定位 + 声明值 + 实际值），使模型能核对
# 「按正文应用」的结果；不再有 ``error``/``reason`` 通道（那是旧拒绝语义）。
# 缺陷类型：接受却无提示，或提示残缺导致模型无法核对。
# 2026-09-21 契约 B：原「error + reason 双通道」的期望已过时。
def test_model_visible_note_guides_the_recomputation(tmp_path: Path) -> None:
    (tmp_path / "mod.txt").write_text("a\nb\n", encoding="utf-8")
    patch = _section("@@ -1,2 +1,2 @@", " a", "-b", "+c", " d")

    observation = ApplyPatchTool().execute(_context(tmp_path), patch=patch)

    assert observation.status == "success"
    assert observation.error is None
    assert observation.content is not None
    assert "note: hunk headers declared counts that did not match the hunk body" in (
        observation.content
    )
    assert "file section 1 hunk 1: '@@ -1,2 +1,2 @@'" in observation.content
    assert "declared source=2 target=2" in observation.content
    assert "body has source=3 target=3" in observation.content
    # 提示只是提示：补丁已按正文（3/3）应用，文件确实被写入为 'a\nc\nd\n'。
    assert (tmp_path / "mod.txt").read_text(encoding="utf-8") == "a\nc\nd\n"


# 目的：计数校验不得抢在更早阶段之前报错（阶段优先级契约）。
# 覆盖非 diff / 空补丁 / 合法但无 change 的补丁。
# 都要走原本的诊断，而不是被计数诊断吞掉。缺陷类型：新增校验提前返回导致错误归因错位。
def test_count_guard_does_not_preempt_earlier_validation_stages() -> None:
    cases = {
        "empty patch": ("", "patch must be a non-empty Git unified diff"),
        "whitespace only": ("   \n", "patch must be a non-empty Git unified diff"),
        "non-diff text": ("not a diff", "expected a Git 'diff --git' header"),
        "missing hunks": (
            "diff --git a/mod.txt b/mod.txt\n--- a/mod.txt\n+++ b/mod.txt\n",
            "each file section must contain exactly one '---', one '+++', and at least one "
            "'@@' hunk",
        ),
        "context-only hunk": (
            _section("@@ -1,1 +1,1 @@", " only"),
            "hunk does not change file contents",
        ),
    }

    for name, (patch, expected) in cases.items():
        operations, error = parse_git_unified_diff(patch)

        assert operations == [], name
        assert error is not None and expected in error, f"{name}: got {error!r}"
        assert not _legacy_count_rejection(error), f"{name}: 计数诊断越权抢占了更早阶段的错误"


# 目的：创建 / 删除 / 重命名这些非「修改既有文件」的 section 不得因计数诊断而改写错误归因。
# 缺陷类型：计数校验先于 header 语义校验，把「不支持的操作」误报成「计数不符」。
def test_unsupported_file_operations_are_not_reported_as_count_mismatches() -> None:
    patches = {
        "creation": "\n".join(
            (
                "diff --git a/new.txt b/new.txt",
                "new file mode 100644",
                "--- /dev/null",
                "+++ b/new.txt",
                "@@ -0,0 +1 @@",
                "+new",
            )
        ),
        "deletion": "\n".join(
            (
                "diff --git a/mod.txt b/mod.txt",
                "deleted file mode 100644",
                "--- a/mod.txt",
                "+++ /dev/null",
                "@@ -1 +0,0 @@",
                "-old",
            )
        ),
    }

    for name, patch in patches.items():
        operations, error = parse_git_unified_diff(patch)

        assert operations == [], name
        assert error is not None, name
        assert not _legacy_count_rejection(error), f"{name}: 误报为计数不符：{error!r}"


# 目的：同一文件出现两个 section 必须被拒（避免重复应用），且不得是计数误报。
# 缺陷类型：去重分支被计数校验掩盖。
def test_duplicate_sections_are_rejected_without_count_confusion() -> None:
    patch = "\n".join(
        (
            _section("@@ -1,2 +1,2 @@", " a", "-b", "+c"),
            _section("@@ -4,2 +4,2 @@", " d", "-e", "+f"),
        )
    )

    operations, error = parse_git_unified_diff(patch)

    assert operations == []
    assert error is not None
    assert "duplicate" in error, f"重复 section 必须给出重复诊断：{error!r}"
    assert not _legacy_count_rejection(error), f"重复 section 被误报为计数不符：{error!r}"


# --------------------------------------------------------------------------------------
# 攻击面 5：幂等 / 无副作用
# --------------------------------------------------------------------------------------


# 目的：解析函数无副作用、可重复调用结果一致、不修改入参。缺陷类型：缓存污染 /
# 原地改写字符串（Python str 不可变，但退化实现可能改用 list）。
# 2026-09-21 契约 B：计数不符的补丁现在被接受（重算），故断言改为「结果一致且无重算差异」。
def test_parsing_is_pure_and_idempotent() -> None:
    patch = _section("@@ -1,2 +1,2 @@", " a", "-b", "+c", " d")
    original = patch

    results = [parse_git_unified_diff_detailed(patch) for _ in range(5)]

    assert patch == original, "解析不得修改入参"
    assert all(outcome.error is None and outcome.operations for outcome in results)
    assert len({tuple(outcome.count_repairs) for outcome in results}) == 1, (
        "重复调用必须给出一致的重算提示"
    )
    assert len(results[0].count_repairs) == 1, "计数不符必须产出重算提示"


def test_hunk_count_diagnostics_is_pure_and_idempotent() -> None:
    section = _section("@@ -1,2 +1,2 @@", " a", "-b", "+c", " d")
    original = section

    results = [_hunk_count_diagnostics(section, 0) for _ in range(5)]

    assert section == original, "诊断函数不得修改入参"
    assert all(diagnostics == results[0] for diagnostics in results)


# 目的：诊断函数的返回类型契约必须稳定（list[str]，可迭代且每个元素是 str）。缺陷类型：返回 None /
# 单字符串导致调用方解包错。
def test_hunk_count_diagnostics_returns_list_of_strings() -> None:
    mismatched = _hunk_count_diagnostics(_section("@@ -1,2 +1,2 @@", " a", "-b", "+c", " d"), 0)
    consistent = _hunk_count_diagnostics(_section("@@ -1,2 +1,2 @@", " a", "-b", "+c"), 0)

    assert isinstance(mismatched, list) and all(isinstance(item, str) for item in mismatched)
    assert consistent == []


# 目的：无 hunk（纯头部）的 section 上诊断函数必须安全返回空列表，不得抛异常。缺陷类型：headers
# 为空时索引越界。
def test_hunk_count_diagnostics_handles_section_without_hunks() -> None:
    section = "diff --git a/mod.txt b/mod.txt\n--- a/mod.txt\n+++ b/mod.txt\n"

    assert _hunk_count_diagnostics(section, 0) == []


# 目的：无换行标记行（``\ No newline at end of file``）单独构成正文时不得被计入任何一侧。
# 缺陷类型：把反斜杠行当作 context。
def test_no_newline_marker_alone_does_not_add_counts() -> None:
    section = _section(
        "@@ -1,1 +1,1 @@",
        "-a",
        "+b",
        "\\ No newline at end of file",
    )

    assert _hunk_count_diagnostics(section, 0) == []


# 目的：``patch`` 参数为非字符串（解析器公开签名声明 str）时不得抛异常。缺陷类型：缺少类型防御导致
# TypeError 穿透。
def test_non_string_patch_is_rejected_without_raising() -> None:
    for value in (None, 42, b"diff --git a/x b/x", ["diff --git a/x b/x"]):
        operations, error = parse_git_unified_diff(value)  # type: ignore[arg-type]

        assert operations == []
        assert error is not None


# --------------------------------------------------------------------------------------
# 交叉校验：小规模穷举，把「计数口径」与 unidiff 的真实判定逐一对齐
# --------------------------------------------------------------------------------------


# 目的：穷举头部×正文组合，只要 unidiff 接受且自洽，本校验就不得拒绝（误拒回归网）。
# 缺陷类型：任何口径偏差。
# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示（不再拒绝）。因此「是否上报了头部/正文不符」
# 的判定从「error 是否出现旧计数拒绝文案」改为「被拒绝，或 count_repairs 非空」——两者都不成立
# 才是真正的漏报。判定基准仍是「头部声明的行数 == 作者写下的正文行数」。
def test_exhaustive_small_grid_never_false_rejects() -> None:
    headers = [
        "@@ -1,1 +1,1 @@",
        "@@ -1,2 +1,2 @@",
        "@@ -1,2 +1,3 @@",
        "@@ -1,3 +1,2 @@",
        "@@ -1 +1 @@",
        "@@ -0,0 +1,2 @@",
        "@@ -1,1 +0,0 @@",
    ]
    bodies = [
        [" a"],
        ["-a", "+b"],
        [" a", "-b", "+c"],
        [" a", ""],
        ["", " a"],
        ["", "-b", "+c"],
        [" \ttab"],
        [" a\r", "-b", "+c"],
        ["-a", "\\ No newline at end of file", "+b"],
        ["+only", "+more"],
    ]

    false_rejections: list[str] = []
    missed_mismatches: list[str] = []
    false_accepts: list[str] = []
    for header, body in itertools.product(headers, bodies):
        patch = _section(header, *body)
        outcome = parse_git_unified_diff_detailed(patch)
        operations, error = outcome.operations, outcome.error
        accepted_by_unidiff, detail = _unidiff_verdict(patch)
        # 契约 B：不符（mismatch）的上报方式有二——(a) 被拒绝，(b) 被重算并给出 count_repairs。
        reported = error is not None or bool(outcome.count_repairs)
        consistent = _body_exactly_matches_declared(header, _serialized_body(header, body))
        if consistent and _legacy_count_rejection(error):
            false_rejections.append(
                f"{header} {body!r}: header matches body but guard rejected: {error!r}"
            )
        if consistent and outcome.count_repairs:
            false_rejections.append(
                f"{header} {body!r}: header matches body but a repair was reported: "
                f"{outcome.count_repairs!r}"
            )
        if not consistent and not reported:
            missed_mismatches.append(
                f"{header} {body!r}: header/body mismatch not reported "
                f"(error={error!r}, repairs={outcome.count_repairs!r})"
            )
        if not accepted_by_unidiff and operations and not outcome.count_repairs:
            false_accepts.append(
                f"{header} {body!r}: unidiff rejects ({detail}) while the parser accepted "
                f"without recomputing any count"
            )

    assert not false_rejections, "误拒（基准=头部与正文自洽）：\n" + "\n".join(false_rejections)
    assert not missed_mismatches, "漏报（头部与正文不符却放行）：\n" + "\n".join(missed_mismatches)
    assert not false_accepts, "漏放（unidiff 拒绝但解析器放行）：\n" + "\n".join(false_accepts)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
