"""Round-5 adversarial probe for the apply_patch patch pipeline (contract + attribution).

Scope (frozen production code; this file only ADDS tests):

1. ``ApplyPatchArgs.model_fields['patch'].description`` now states a structure rule that was
   previously absent from every model-visible text: "each file section must carry exactly one
   '---' header, exactly one '+++', and at least one '@@' hunk". The rule is attacked on two
   fronts: (a) is it consistent with ``patch_parser._validate_section_headers`` for every
   concrete shape (missing '---', missing '+++', two '---', no '@@', mixed multi-section);
   (b) is it a TRUE statement about the format the tool claims to accept ("Git-style unified
   diff"), given that ``git apply`` accepts several headerless section shapes.
2. The tool description (``apply_patch_tool.APPLY_PATCH_DESCRIPTION``) no longer restates the
   prefix rule, the count rule, the structure rule, or the ``*** Begin Patch`` prohibition; it
   carries the tool's responsibility and points at the ``patch`` parameter description, which
   is the single source of truth for the format. Every constraint must remain derivable from
   "tool description + parameter description" alone.
3. ``validate_all``'s failure ``reason`` is now attributed per cause class. All five classes
   (missing/irregular file, non-UTF-8, binary, ``.cosir`` reserved area, hunk context
   mismatch) are probed for a reason that is true and actionable, and for the absence of
   wording that pushes the model toward an unfixable direction.
4. Strength of the de-duplication assertions: can a paraphrase smuggle the count rule back into
   the tool description? Verified with a read-only mutation (no production file is touched).
5. Routine adversarial regression: multi-file / multi-hunk, ``index`` lines, ``\\ No newline``
   in every position, CRLF/CR, tab, trailing spaces, BOM, ``@@`` with section text, very long
   lines, explicit 0, diagnostic quality, safety (never write on rejection, ``retryable``,
   no resource lock for an invalid patch), purity / idempotence.
"""

from __future__ import annotations

import itertools
import pathlib

import pytest

from app.core.tools.guard.file_resource_paths import FileResourceResolver
from app.core.tools.schemas import ToolExecutionContext
from app.core.tools.tool_handler.apply_patch_tool import APPLY_PATCH_DESCRIPTION, ApplyPatchTool
from app.core.tools.tool_handler.patch_write.patch_parser import (
    parse_git_unified_diff,
    parse_git_unified_diff_detailed,
)
from app.core.tools.tool_models.apply_patch_args import ApplyPatchArgs

PARAM_DESCRIPTION = ApplyPatchArgs.model_fields["patch"].description or ""

# Every phrase the tool description uses to state the structure rule. The rule is only
# meaningful if all three clauses are present and no clause is weakened.
STRUCTURE_CLAUSES = (
    "each file section must carry exactly",
    "one '---' header",
    "exactly one '+++'",
    "at least one '@@' hunk",
)


def _section(path: str, *lines: str) -> str:
    """Build one Git file section: three header lines then the given body lines."""

    return "\n".join((f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", *lines))


def _valid_patch(path: str = "m.txt") -> str:
    return _section(path, "@@ -1,1 +1,1 @@", "-a", "+A") + "\n"


def _context(root: pathlib.Path, run_id: int = 5001) -> ToolExecutionContext:
    return ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=root, run_id=run_id)


def _execute(root: pathlib.Path, patch: str, run_id: int = 5001):
    return ApplyPatchTool().execute(_context(root, run_id), patch=patch)


def _tool_bytes(
    tmp_path: pathlib.Path,
    patch: str,
    content: str,
    run_id: int,
    relative: str = "m.txt",
) -> tuple[str, bytes]:
    """Seed ``relative`` with ``content`` inside ``tmp_path`` and run the tool on ``patch``."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content.encode("utf-8"))
    observation = _execute(tmp_path, patch, run_id)
    return observation.status, target.read_bytes()


# ---------------------------------------------------------------------------
# Attack surface 1: is the newly documented structure rule a TRUE contract?
# ---------------------------------------------------------------------------


# Purpose: ``git apply`` -- the authority the description invokes via "Git-style unified
# diff" -- accepts a mode-only and a rename-only section that carry NO '---', NO '+++', and
# NO '@@'. Measured with real Git 2.x in a scratch repo: both `git apply --check` runs exit
# 0 ("Checking patch m.txt...", "Checking patch m.txt => n.txt...").
# Defect type: the model-visible rule overstates git's format, so a git-shaped diff is
# described as impossible while it is not. If the maintainer runtime ever used Git, such a
# diff could arrive, be rejected by the parser, and the description gives the model no way
# to predict this.
def test_documented_hunk_rule_is_framed_as_this_tool_acceptance_surface() -> None:
    headerless_but_valid_git_sections = {
        "mode-only (git accepts, verified): old mode / new mode / index": (
            "diff --git a/m.txt b/m.txt\nold mode 100644\nnew mode 100755\n"
            "index ce01362..ce01362\n"
        ),
        "rename-only (git accepts, verified): similarity index / rename from / rename to": (
            "diff --git a/m.txt b/n.txt\nsimilarity index 100%\n"
            "rename from m.txt\nrename to n.txt\n"
        ),
    }

    # 这些形状是合法 Git diff，但本工具只改内容：解析器必须以「能力限制」的理由拒绝，
    # 不能让模型以为它们不是 Git diff。
    for name, section in headerless_but_valid_git_sections.items():
        operations, error = parse_git_unified_diff(section)
        assert operations == [], f"{name}: 本工具不得接受无内容 hunk 的 section"
        assert "not supported" in str(error), f"{name}: 拒绝理由须为能力限制，实际 {error!r}"

    # 描述里的结构规则必须写在「本工具只接受内容 hunk」之后，作为接受面限制而非 Git 格式规则。
    framing = PARAM_DESCRIPTION.index("accepts content hunks only")
    rule = PARAM_DESCRIPTION.index("at least one '@@' hunk")
    assert framing < rule, "结构规则须作为本工具接受面限制表述，而不是 Git 的格式规则"


# Purpose: the structure rule must have exactly one wording. A second, weaker statement
# ("hunks are optional" style) anywhere in the model-visible text would reopen the drift the
# rule was added to close. Defect type: the new rule is hedged elsewhere.
def test_structure_rule_is_stated_once_and_unhedged() -> None:
    both = APPLY_PATCH_DESCRIPTION + "\n" + PARAM_DESCRIPTION
    lowered = both.lower()

    missing = [clause for clause in STRUCTURE_CLAUSES if clause not in PARAM_DESCRIPTION]

    assert not missing, f"结构规则的必需子句缺失: {missing}"
    assert PARAM_DESCRIPTION.count("at least one '@@' hunk") == 1, "结构规则重复陈述"
    # No hedge that would let the model skip headers/hunks.
    hedges = (
        "optional '@@'",
        "no '@@' hunk is needed",
        "headers are optional",
        "no '---' is required",
    )
    for hedge in hedges:
        assert hedge.lower() not in lowered, f"结构规则被对冲: {hedge!r}"


# Purpose: the structure rule lives only in the parameter description (single source of truth),
# and the tool description must not carry any of its fragments. Defect type: duplicated rule.
def test_structure_rule_lives_only_in_the_parameter_description() -> None:
    for needle in ("diff --git", "---", "+++", "@@", "exactly one"):
        assert needle in PARAM_DESCRIPTION, f"参数描述缺少 {needle!r}"
    for token in (
        "exactly one '---'",
        "exactly one '+++'",
        "at least one '@@'",
        "---",
        "+++",
        "@@",
    ):
        assert token not in APPLY_PATCH_DESCRIPTION, f"工具描述重复了结构规则片段: {token!r}"


# ---------------------------------------------------------------------------
# Attack surface 2: contract vs. implementation, shape by shape
# ---------------------------------------------------------------------------


# Purpose: feed the documented structure rule's positive and negative shapes through the
# real validator. Positive: exactly one '---', one '+++', >=1 '@@'. Negative: missing '---',
# missing '+++', two '---', two '+++', no '@@'. Defect type: "描述承诺而实现拒绝" or the
# reverse.
_ONE_HEADER_EACH = _section("m.txt", "@@ -1,1 +1,1 @@", "-a", "+A")
_TWO_HUNKS = _section("m.txt", "@@ -1,1 +1,1 @@", "-a", "+A", "@@ -5,1 +5,1 @@", "-b", "+B")
_MISSING_OLD = "diff --git a/m.txt b/m.txt\n+++ b/m.txt\n@@ -1,1 +1,1 @@\n-a\n+A"
_MISSING_NEW = "diff --git a/m.txt b/m.txt\n--- a/m.txt\n@@ -1,1 +1,1 @@\n-a\n+A"
_TWO_OLD = (
    "diff --git a/m.txt b/m.txt\n--- a/m.txt\n--- a/other.txt\n"
    "+++ b/m.txt\n@@ -1,1 +1,1 @@\n-a\n+A"
)
_TWO_NEW = (
    "diff --git a/m.txt b/m.txt\n--- a/m.txt\n+++ b/m.txt\n+++ b/other.txt\n"
    "@@ -1,1 +1,1 @@\n-a\n+A"
)
_NO_HUNK = "diff --git a/m.txt b/m.txt\n--- a/m.txt\n+++ b/m.txt\n"

STRUCTURE_SHAPES = (
    ("positive: one '---', one '+++', one '@@'", _ONE_HEADER_EACH, True),
    ("positive: two '@@' hunks", _TWO_HUNKS, True),
    ("negative: missing '---'", _MISSING_OLD, False),
    ("negative: missing '+++'", _MISSING_NEW, False),
    ("negative: two '---' headers", _TWO_OLD, False),
    ("negative: two '+++' headers", _TWO_NEW, False),
    ("negative: no '@@' hunk at all", _NO_HUNK, False),
)


@pytest.mark.parametrize(("name", "section", "accepted"), STRUCTURE_SHAPES)
def test_documented_structure_shapes_match_the_validator(
    name: str, section: str, accepted: bool
) -> None:
    operations, error = parse_git_unified_diff(section + "\n")

    if accepted:
        assert error is None and operations, f"{name}: 描述承诺接受但实现拒绝: {error!r}"
    else:
        assert operations == [] and error is not None, f"{name}: 描述承诺拒绝但实现接受"


# Purpose: the structure rule is stated per file section, so a mixed patch must be judged
# section by section: one good section plus one headerless section must be rejected as a
# whole, and the diagnosis must not blame the good section. Defect type: multi-section rule
# applied to the first section only.
def test_mixed_multi_section_rule_is_enforced_section_by_section() -> None:
    good = _section("good.txt", "@@ -1,1 +1,1 @@", "-a", "+A")
    headerless = "diff --git a/bad.txt b/bad.txt\n@@ -1,1 +1,1 @@\n-a\n+A"
    patch = good + "\n" + headerless + "\n"

    operations, error = parse_git_unified_diff(patch)

    assert operations == [], "含无 '---'/'+++' section 的补丁必须整体被拒"
    assert error is not None
    assert (
        "exactly one '---', one '+++', and at least one '@@' hunk" in error
    ), f"未按结构规则拒绝混合补丁: {error!r}"
    assert "file section 1" not in error, f"诊断错误归因到合法的第 1 节: {error!r}"


# Purpose: the reject message the model sees must be the same rule the description teaches,
# character for character on the rule clause, so the model can map failure -> contract.
# Defect type: description and rejection message have drifted.
def test_rejection_message_matches_the_documented_rule_wording() -> None:
    headerless = "diff --git a/m.txt b/m.txt\n@@ -1,1 +1,1 @@\n-a\n+A\n"

    _, error = parse_git_unified_diff(headerless)

    assert error is not None
    assert "exactly one '---', one '+++', and at least one '@@' hunk" in error
    # The parameter description states the same rule with the same cardinalities.
    assert "exactly one '---' header" in PARAM_DESCRIPTION
    assert "exactly one '+++'" in PARAM_DESCRIPTION
    assert "at least one '@@' hunk" in PARAM_DESCRIPTION


# ---------------------------------------------------------------------------
# Attack surface 3: reason attribution for all five validation-failure classes
# ---------------------------------------------------------------------------


# Purpose: sample every ``validate_all`` failure class through the real tool and collect the
# (error, reason) pair. Defect type: a cause class whose reason is untrue or unactionable.
def _validation_cases(tmp_path: pathlib.Path) -> dict[str, tuple[str, str]]:
    (tmp_path / ".cosir").mkdir(exist_ok=True)
    (tmp_path / ".cosir" / "meta.txt").write_text("x\n", encoding="utf-8")
    (tmp_path / "bin.txt").write_bytes(b"a\x00b\n")
    (tmp_path / "latin.txt").write_bytes(b"caf\xe9\n")
    (tmp_path / "ctx.txt").write_bytes(b"keep\nold\n")
    (tmp_path / "adir").mkdir(exist_ok=True)

    return {
        "missing file": (_valid_patch("missing.txt"), "not an existing regular file"),
        "directory target": (_valid_patch("adir"), "not an existing regular file"),
        "binary target": (_valid_patch("bin.txt"), "binary files are not supported"),
        "non-UTF-8 target": (_valid_patch("latin.txt"), "not a readable UTF-8 text file"),
        "reserved area": (_valid_patch(".cosir/meta.txt"), "reserved"),
        "hunk context mismatch": (
            _section("ctx.txt", "@@ -1,1 +1,1 @@", "-absent", "+A") + "\n",
            "hunk 1 not found",
        ),
    }


# Purpose: each validation failure must (a) name its cause in ``error`` and (b) attribute it
# in ``reason`` without pointing at a fix that cannot work. Defect type: reason attribution
# error (the round-4 finding's regression guard).
def test_every_validation_cause_is_attributed_in_the_reason(tmp_path: pathlib.Path) -> None:
    expectations = {
        "missing file": ("missing or irregular file",),
        "directory target": ("missing or irregular file",),
        "binary target": ("binary content",),
        "non-UTF-8 target": ("non-UTF-8 bytes",),
        "reserved area": ("reserved or blocked path",),
        "hunk context mismatch": ("then submit a new Git unified diff",),
    }

    failures = []
    for index, (name, (patch, error_needle)) in enumerate(
        _validation_cases(tmp_path).items(), start=1
    ):
        observation = _execute(tmp_path, patch, 5100 + index)
        reason = observation.reason or ""
        if observation.status != "error":
            failures.append(f"{name}: status={observation.status} 期望 error")
            continue
        if error_needle not in observation.error:
            failures.append(f"{name}: error 未点名原因 {error_needle!r}: {observation.error!r}")
        for needle in expectations[name]:
            if needle not in reason:
                failures.append(f"{name}: reason 未按原因类别归因 {needle!r}: {reason!r}")

    assert not failures, "reason 归因缺陷:\n" + "\n".join(failures)


# Purpose: the reason must never push a model toward an unfixable direction. For target-file
# causes (binary / encoding / reserved) the reason must say plainly that editing the diff
# cannot help. Defect type: misleading repair guidance.
def test_target_file_causes_say_the_diff_cannot_fix_them(tmp_path: pathlib.Path) -> None:
    cases = _validation_cases(tmp_path)

    failures = []
    for index, name in enumerate(("binary target", "non-UTF-8 target", "reserved area"), start=1):
        patch, _ = cases[name]
        observation = _execute(tmp_path, patch, 5200 + index)
        reason = observation.reason or ""
        if "cannot be fixed by editing the diff" not in reason:
            failures.append(f"{name}: reason 未说明「改 diff 无法修复」: {reason!r}")
        if "pick another target" not in reason:
            failures.append(f"{name}: reason 未给出「换目标」这一可行方向: {reason!r}")

    assert not failures, "target 类原因仍可能把模型引向不可修正方向:\n" + "\n".join(failures)


# Purpose: a hunk-context failure IS fixable by editing the diff, so the reason must not
# wrongly tell the model to abandon the target. Defect type: over-broad reason that removes
# the actionable repair path.
def test_hunk_context_failure_keeps_the_diff_repair_path_open(tmp_path: pathlib.Path) -> None:
    patch, _ = _validation_cases(tmp_path)["hunk context mismatch"]

    observation = _execute(tmp_path, patch, 5300)

    assert observation.status == "error"
    assert "no files were modified" in observation.error
    reason = observation.reason or ""
    assert (
        "then submit a new Git unified diff" in reason
    ), f"可修正的上下文不符原因被剥夺了「改 diff 重提」的修复路径: {reason!r}"


# Purpose: the rejected validation path must not have written anything and must stay
# retryable, for every cause class. Defect type: rejection with write side effects.
def test_no_validation_cause_writes_a_file_or_blocks_retry(tmp_path: pathlib.Path) -> None:
    # Materialize every fixture first: the case builder creates the files it needs.
    cases = _validation_cases(tmp_path)
    snapshots: dict[str, bytes] = {}
    for path in ("bin.txt", "latin.txt", "ctx.txt"):
        snapshots[path] = (tmp_path / path).read_bytes()
    before_cosir = (tmp_path / ".cosir" / "meta.txt").read_bytes()

    failures = []
    for index, (name, (patch, _)) in enumerate(cases.items(), start=1):
        observation = _execute(tmp_path, patch, 5400 + index)
        if observation.retryable is not True:
            failures.append(f"{name}: retryable={observation.retryable} 期望 True")
        if observation.error.count("no files were modified") != 1:
            failures.append(f"{name}: error 未声明零写入: {observation.error!r}")

    for path, expected in snapshots.items():
        target = tmp_path / path
        if target.read_bytes() != expected:
            failures.append(f"{path}: 校验失败路径改写了文件")
    if (tmp_path / ".cosir" / "meta.txt").read_bytes() != before_cosir:
        failures.append(".cosir/meta.txt: 校验失败路径改写了保留区文件")

    assert not failures, "校验失败路径出现写副作用 / 可重试语义漂移:\n" + "\n".join(failures)


# Purpose: ``.cosir`` is a safety block, so the model-visible error must not leak a way to
# bypass it (e.g. a suggestion to use a relative path that escapes, or to allow_reserved).
# Defect type: safety guidance leak.
def test_reserved_area_error_does_not_hint_at_a_bypass(tmp_path: pathlib.Path) -> None:
    (tmp_path / ".cosir").mkdir(exist_ok=True)
    (tmp_path / ".cosir" / "meta.txt").write_text("x\n", encoding="utf-8")

    observation = _execute(tmp_path, _valid_patch(".cosir/meta.txt"), 5500)
    text = (observation.error or "") + "\n" + (observation.reason or "")
    lowered = text.lower()

    assert observation.status == "error"
    for bypass in ("allow_reserved", "bypass", "override", "force", "disable"):
        assert bypass not in lowered, f"保留区错误信息暗示了绕过手段 {bypass!r}: {text!r}"


# ---------------------------------------------------------------------------
# Attack surface 4: the tool description must stay free of format semantics
# (read-only mutation; no production file is touched)
# ---------------------------------------------------------------------------


# Semantic tokens any restatement of the diff format needs. The guard is deliberately
# structural (not a fixed wording list) so a synonym rewrite is caught too.
_TOOL_DESCRIPTION_FORBIDDEN_TOKENS = (
    "counts",
    "must match",
    "line count",
    "context plus",
    "hunk",
    "declared source",
    "declared target",
    "count exactly",
    "figures",
    "equal",
    "number of lines",
    "header",
    "starts with",
    "removed",
    "added",
    "diff --git",
    "*** Begin Patch",
    "@@",
)


# Purpose: the tool description carries the responsibility and the boundary only; not a single
# piece of the diff format may leak into it. Defect type: dual source of truth.
def test_tool_description_carries_no_format_semantics() -> None:
    lowered = APPLY_PATCH_DESCRIPTION.lower()

    leaked = [token for token in _TOOL_DESCRIPTION_FORBIDDEN_TOKENS if token in lowered]

    assert not leaked, f"工具描述出现了格式/计数语义: {leaked}"


# Purpose: whatever wording a future editor uses to copy the format rule back into the tool
# description, the semantic guard must catch it. Defect type: keyword-only guard bypass.
@pytest.mark.parametrize(
    "paraphrase",
    [
        "'source count' is context plus '-' lines and 'target count' is context plus '+' lines.",
        "A line count may be omitted when it is 1.",
        "The header numbers correspond to the body lines exactly.",
        "Header figures have to agree with the lines they introduce.",
        "Each header's two figures must equal the number of lines it covers.",
        "Copy the block that starts with 'diff --git'.",
    ],
)
def test_semantic_guard_blocks_every_format_rule_paraphrase(paraphrase: str) -> None:
    mutated = (APPLY_PATCH_DESCRIPTION + " " + paraphrase).lower()

    leaked = [token for token in _TOOL_DESCRIPTION_FORBIDDEN_TOKENS if token in mutated]

    assert leaked, f"格式规则被同义改写抄回工具描述而未被拦下: {paraphrase!r}"


# Purpose: the prefix rule must live in the parameter description with its three literal
# prefixes (context / removed / added), and nowhere else. Defect type: information loss or
# dual source during de-duplication.
def test_prefix_rule_lives_in_the_parameter_description() -> None:
    assert "every line starts with" in PARAM_DESCRIPTION
    for prefix in ("-", "+", " "):
        assert f"'{prefix}'" in PARAM_DESCRIPTION, f"前缀规则缺少 {prefix!r}"
    assert "every line starts with" not in APPLY_PATCH_DESCRIPTION


# Purpose: the parser must implement exactly the prefix set the description teaches: a body
# line whose first character is none of ' ', '-', '+' must be rejected with a diagnosis about
# the body, not about counts. Defect type: undocumented accepted shape.
@pytest.mark.parametrize("intruder", ["?not-a-prefix", "x: stray"])
def test_body_prefix_set_matches_the_documented_rule(intruder: str) -> None:
    section = _section("m.txt", "@@ -1,2 +1,2 @@", " a", intruder) + "\n"

    operations, error = parse_git_unified_diff(section)

    assert operations == [], f"未文档化的正文前缀被接受: {intruder!r}"
    assert error is not None
    assert "outside a unified-diff hunk" in error, f"诊断归因错误: {error!r}"


# Purpose: only the exact Git marker ``\ No newline at end of file`` marks a body line as
# metadata. A body line that merely *starts* with a backslash (``\x``, ``\t``, a literal
# backslash in the source) must be either counted as content or rejected - never silently
# treated as an EOF marker. Defect type: content line consumed as metadata (silent loss).
@pytest.mark.parametrize("body_line", ["\\x", "\\typo", "\\", "\\\\"])
def test_backslash_led_body_lines_are_not_treated_as_eof_markers(body_line: str) -> None:
    # Header is self-consistent under the *documented* rule: two body lines, both context.
    section = _section("m.txt", "@@ -1,2 +1,2 @@", " a", body_line) + "\n"

    operations, error = parse_git_unified_diff(section)

    # Either the line is content (accepted, and it is present in the parsed hunk), or the
    # patch is rejected. What must never happen is "accepted while the line vanished".
    parsed_contents = [
        line.content for operation in operations for hunk in operation.hunks for line in hunk.lines
    ]

    accepted_without_content = error is None and body_line[1:] not in parsed_contents
    assert not accepted_without_content, (
        f"以反斜杠开头的正文行 {body_line!r} 被当作无尾换行标记静默丢弃，"
        f"而补丁整体被判为有效：parsed={parsed_contents!r}"
    )


# Purpose: the documented prefix rule says every body line starts with ' ', '-' or '+'. The
# implementation additionally accepts '\\' and '@'. Verify the extra accepted prefixes are
# at least harmless (never silently dropped) once the patch is judged valid.
# Defect type: description/implementation mismatch with a real content effect.
def test_extra_accepted_body_prefixes_never_drop_content() -> None:
    # '\\' line under a header that the implementation counts as satisfying 1/1.
    accepted_shapes = (
        _section("m.txt", "@@ -1,1 +1,1 @@", "-a", "+b", "\\ No newline at end of file") + "\n",
        _section("m.txt", "@@ -1,1 +1,1 @@", "-a", "+b", "\\x") + "\n",
    )
    failures = []
    for patch in accepted_shapes:
        operations, error = parse_git_unified_diff(patch)
        if error is not None:
            continue
        contents = [
            line.content
            for operation in operations
            for hunk in operation.hunks
            for line in hunk.lines
        ]
        if "b" not in contents:
            failures.append(f"{patch!r} -> {contents!r}")

    assert not failures, "被接受的补丁丢失了 '+b' 内容行:\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# Attack surface 5: routine adversarial regression
# ---------------------------------------------------------------------------


# Purpose: multi-file / multi-hunk / index line / tab / trailing spaces / '@@' with section
# text / very long line must stay accepted and written faithfully. Defect type: acceptance
# drift caused by the round-5 edits.
def test_routine_shapes_stay_accepted_and_faithful(tmp_path: pathlib.Path) -> None:
    cases = {
        "index line": (
            "diff --git a/f.txt b/f.txt\nindex 0000000..1111111 100644\n"
            "--- a/f.txt\n+++ b/f.txt\n@@ -1,1 +1,1 @@\n-a\n+A\n",
            "f.txt",
            "a\n",
            b"A\n",
        ),
        "two hunks in order": (
            _section("g.txt", "@@ -1,1 +1,1 @@", "-a", "+A", "@@ -3,1 +3,1 @@", "-c", "+C") + "\n",
            "g.txt",
            "a\nb\nc\n",
            b"A\nb\nC\n",
        ),
        "tab indented context": (
            _section("h.txt", "@@ -1,2 +1,2 @@", " \tkeep", "-a", "+A") + "\n",
            "h.txt",
            "\tkeep\na\n",
            b"\tkeep\nA\n",
        ),
        "trailing spaces preserved": (
            _section("i.txt", "@@ -1,2 +1,2 @@", " a  ", "-b  ", "+c  ") + "\n",
            "i.txt",
            "a  \nb  \n",
            b"a  \nc  \n",
        ),
        "section text after @@": (
            _section("j.txt", "@@ -1,2 +1,2 @@ def f():", " a", "-b", "+c") + "\n",
            "j.txt",
            "a\nb\n",
            b"a\nc\n",
        ),
        "very long line": (
            _section("k.txt", "@@ -1,1 +1,1 @@", "-" + "x" * 5000, "+" + "y" * 5000) + "\n",
            "k.txt",
            "x" * 5000 + "\n",
            ("y" * 5000 + "\n").encode("utf-8"),
        ),
    }

    failures = []
    for index, (name, (patch, relative, before, after)) in enumerate(cases.items(), start=1):
        status, written = _tool_bytes(
            tmp_path / f"case{index}", patch, before, 5600 + index, relative
        )
        if status != "success":
            failures.append(f"{name}: status={status}")
        elif written != after:
            failures.append(f"{name}: wrote={written!r} expected={after!r}")

    assert not failures, "常规形状接受面漂移:\n" + "\n".join(failures)


# Purpose: a multi-file patch must be applied per file, and a failure in file two must leave
# file one untouched (validation happens before any write). Defect type: partial write on a
# rejected multi-file patch.
def test_multi_file_rejection_leaves_every_file_untouched(tmp_path: pathlib.Path) -> None:
    patch = (
        _section("first.txt", "@@ -1,1 +1,1 @@", "-a", "+A")
        + "\n"
        + _section("second.txt", "@@ -1,1 +1,1 @@", "-not-there", "+X")
        + "\n"
    )
    (tmp_path / "first.txt").write_bytes(b"a\n")
    (tmp_path / "second.txt").write_bytes(b"y\n")

    observation = _execute(tmp_path, patch, 5700)

    assert observation.status == "error"
    assert (tmp_path / "first.txt").read_bytes() == b"a\n"
    assert (tmp_path / "second.txt").read_bytes() == b"y\n"


# Purpose: ``\ No newline at end of file`` markers must count on neither side, in every
# position, for a self-consistent header. Defect type: marker counted as a body line.
@pytest.mark.parametrize(
    "body",
    [
        ("-a", "\\ No newline at end of file", "+b"),
        ("-a", "+b", "\\ No newline at end of file"),
        ("-a", "\\ No newline at end of file", "+b", "\\ No newline at end of file"),
        (" a", "-b", "\\ No newline at end of file", "+c"),
    ],
)
def test_no_newline_marker_never_changes_the_count(body: tuple[str, ...]) -> None:
    source = sum(1 for line in body if line[:1] in {" ", "-"})
    target = sum(1 for line in body if line[:1] in {" ", "+"})
    patch = _section("m.txt", f"@@ -1,{source} +1,{target} @@", *body) + "\n"

    operations, error = parse_git_unified_diff(patch)

    assert error is None, f"标记位置 {body} 被误拒: {error!r}"
    assert operations


# Purpose: CRLF patches and CR-only bodies must keep the same count verdict (counts are
# computed from the body, not the raw bytes). Defect type: newline normalization drift.
@pytest.mark.parametrize("variant", ["crlf_whole", "cr_only_body"])
def test_carriage_return_variants_keep_the_count_verdict(variant: str) -> None:
    lf = _section("m.txt", "@@ -1,2 +1,2 @@", " a", "-b", "+c") + "\n"
    patch = (
        lf.replace("\n", "\r\n")
        if variant == "crlf_whole"
        else _section("m.txt", "@@ -1,2 +1,2 @@", " a\r", "-b", "+c") + "\n"
    )

    operations, error = parse_git_unified_diff(patch)

    assert error is None, f"{variant} 被误拒: {error!r}"
    assert operations


# Purpose: a BOM before the patch is a patch-level problem and must never surface as a count
# diagnostic. Defect type: diagnostic misattribution.
def test_bom_prefix_is_never_reported_as_a_count_problem() -> None:
    operations, error = parse_git_unified_diff(
        "\ufeff" + _section("m.txt", "@@ -1,1 +1,1 @@", "-a", "+A")
    )

    assert operations == []
    assert error is not None
    assert "hunk line counts do not match" not in error


# Purpose: explicit ``0`` counts must never be coerced to ``1`` by an ``or 1`` bug, in either
# position, and the parsed lengths must reflect the zero. Defect type: 0 -> 1 coercion.
def test_explicit_zero_counts_are_preserved_in_the_parsed_hunk() -> None:
    insert = _section("m.txt", "@@ -0,0 +1,1 @@", "+a") + "\n"
    delete = _section("m.txt", "@@ -1,1 +0,0 @@", "-a") + "\n"

    insert_ops, insert_error = parse_git_unified_diff(insert)
    delete_ops, delete_error = parse_git_unified_diff(delete)

    assert insert_error is None and insert_ops[0].hunks[0].source_length == 0
    assert insert_error is None and insert_ops[0].hunks[0].source_start == 0
    assert delete_error is None and delete_ops[0].hunks[0].target_length == 0
    assert delete_error is None and delete_ops[0].hunks[0].target_start == 0


# Purpose: count diagnostics must carry the section and hunk indexes, the verbatim header,
# the declared values and the measured values, in stable order and pure ASCII. Defect type:
# incomplete / unstable / non-ASCII diagnostics.
# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示。第 1 个 section 必须改成「自洽但真的改动
# 内容」的 hunk（纯 context 的 hunk 会被 'hunk does not change file contents' 抢先拒绝，与本用例
# 无关）；断言落在 count_repairs 上，字段齐全性与 ASCII 要求不放宽。
def test_count_diagnostics_are_complete_stable_and_ascii() -> None:
    patch = (
        _section("first.txt", "@@ -1,2 +1,2 @@", " ok", "-old", "+new")
        + "\n"
        + _section("second.txt", "@@ -2,2 +2,3 @@", " a", "-b", "+c")
        + "\n"
    )

    first_outcome = parse_git_unified_diff_detailed(patch)
    second_outcome = parse_git_unified_diff_detailed(patch)

    assert first_outcome.error is None, "计数不符不再阻断应用"
    assert len(first_outcome.operations) == 2
    assert first_outcome.count_repairs == second_outcome.count_repairs, "顺序与内容必须稳定"
    repairs = "\n".join(first_outcome.count_repairs)
    assert "file section 2 hunk 1: '@@ -2,2 +2,3 @@'" in repairs
    assert "declared source=2 target=3" in repairs
    assert "body has source=2 target=2" in repairs
    assert "file section 1" not in repairs, "自洽的第 1 个 section 不得出现在提示中"
    repairs.encode("ascii")


# Purpose: an omitted count means exactly one line - verified on both sides of the header so
# the omission rule cannot drift between source and target. Defect type: asymmetric
# omission handling.
@pytest.mark.parametrize(
    ("header", "body", "source", "target"),
    [
        ("@@ -1 +1 @@", ["-a", "+b"], 1, 1),
        ("@@ -1,1 +1 @@", ["-a", "+b"], 1, 1),
        ("@@ -1 +1,1 @@", ["-a", "+b"], 1, 1),
        ("@@ -1,2 +1 @@", ["-a", "-b", "+c"], 2, 1),
    ],
)
def test_omitted_count_means_one_on_either_side(
    header: str, body: list[str], source: int, target: int
) -> None:
    patch = _section("m.txt", header, *body) + "\n"

    operations, error = parse_git_unified_diff(patch)

    assert error is None, f"{header} 被误拒: {error!r}"
    hunk = operations[0].hunks[0]
    assert (hunk.source_length, hunk.target_length) == (source, target)


# Purpose: the count rule must hold for a grid of self-consistent patches (never falsely
# rejected) and for the same grid with one tampered count (always reported). Defect type:
# any systematic口径 deviation in either direction.
# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示（不再拒绝）。篡改计数的补丁因此「被接受且
# count_repairs 非空」——真正的缺陷变成「既未被报出又被静默接受」，断言据此收紧到「必须被报出」。
def test_self_consistent_grid_never_rejected_and_tampered_grid_always_rejected() -> None:
    bodies = [[" a"], ["-a", "+b"], [" a", "-b", "+c"], ["-a", "+b"], [" a", " b", "-c", "+d"]]
    false_rejections: list[str] = []
    unreported_mismatches: list[str] = []
    for body in bodies:
        source = sum(1 for line in body if line[:1] in {" ", "-"})
        target = sum(1 for line in body if line[:1] in {" ", "+"})
        if not any(line[:1] in {"+", "-"} for line in body):
            continue
        honest = _section("m.txt", f"@@ -1,{source} +1,{target} @@", *body) + "\n"
        honest_outcome = parse_git_unified_diff_detailed(honest)
        if honest_outcome.error is not None:
            false_rejections.append(f"{honest!r}: {honest_outcome.error!r}")
        if honest_outcome.count_repairs:
            false_rejections.append(
                f"{honest!r}: 自洽补丁出现重算提示 {honest_outcome.count_repairs!r}"
            )
        for delta_source, delta_target in itertools.product((1, -1), repeat=2):
            tampered_source = source + delta_source
            tampered_target = target + delta_target
            if tampered_source < 0 or tampered_target < 0:
                continue
            if (tampered_source, tampered_target) == (source, target):
                continue
            tampered = (
                _section("m.txt", f"@@ -1,{tampered_source} +1,{tampered_target} @@", *body) + "\n"
            )
            tampered_outcome = parse_git_unified_diff_detailed(tampered)
            reported = tampered_outcome.error is not None or bool(tampered_outcome.count_repairs)
            if not reported:
                unreported_mismatches.append(f"{tampered!r}")

    assert not false_rejections, "自洽补丁被误拒或误报:\n" + "\n".join(false_rejections)
    assert not unreported_mismatches, "头部与正文不符却未被报出:\n" + "\n".join(
        unreported_mismatches
    )


# Purpose: end-to-end, a patch whose body contains a '\\'-led content line must never report
# success while the written file silently lost that line. Either the patch is rejected, or
# the line is written. Defect type: silent content loss reported as success.
def test_backslash_content_line_is_never_silently_dropped_on_success(
    tmp_path: pathlib.Path,
) -> None:
    # The file already contains a line that starts with a backslash; the patch keeps it as
    # context and changes a neighbouring line.
    before = "a\n\\keep\nz\n"
    patch = _section("m.txt", "@@ -1,3 +1,3 @@", " a", " \\keep", "-z", "+Z") + "\n"
    status, written = _tool_bytes(tmp_path, patch, before, 5750)

    if status == "success":
        assert "\\keep" in written.decode("utf-8"), (
            "工具报成功，但以反斜杠开头的正文行被静默丢弃（作者意图丢失）: " f"wrote={written!r}"
        )
    else:
        # Rejection is acceptable, but it must be a count diagnosis the model can act on,
        # not a silent drop.
        assert written == before.encode("utf-8")


# Purpose: the ``\`` marker handling must key on the exact Git marker text, and the
# documented prefix rule (' '/'-'/'+') must describe what the model has to write. If the
# implementation consumes a merely backslash-led line as metadata, the count diagnostic must
# at least make the discrepancy visible instead of accepting the patch.
# Defect type: implementation accepts an undocumented prefix and consumes content.
#
# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示，补丁本身会被接受。因此「静默成功」的判据
# 收紧为：一个以反斜杠开头的**非标记**正文行若被当作元数据消费，则要么它仍出现在解析结果里，
# 要么（因为它不计入任何一侧，头部计数必然与实际不符）解析必须给出 count_repairs 重算提示，
# 使这处内容丢失在模型可见通道中留下痕迹。两者都不成立才是真正的静默成功。
def test_undocumented_backslash_prefix_never_yields_a_silent_success() -> None:
    cases = {
        "pure backslash line": "\\",
        "backslash-t text": "\\t",
        "double backslash": "\\\\",
    }

    silent = []
    for name, body_line in cases.items():
        # 头部声明 target=2（作者以为反斜杠行也是内容行），实际内容行只有 -a/+b 一行往返。
        patch = _section("m.txt", "@@ -1,1 +1,2 @@", "-a", "+b", body_line) + "\n"
        outcome = parse_git_unified_diff_detailed(patch)
        if outcome.error is not None:
            continue
        contents = [
            line.content
            for operation in outcome.operations
            for hunk in operation.hunks
            for line in hunk.lines
        ]
        if body_line in contents:
            continue
        if not outcome.count_repairs:
            silent.append(
                f"{name}: patch accepted without any repair note and {body_line!r} "
                f"missing from parsed lines: {contents!r}"
            )
        else:
            # 提示必须点名该补丁的计数重算，而不是空泛成功。
            assert "declared source=1 target=2" in outcome.count_repairs[0], (
                f"{name}: 提示未点出被消费的正文行造成的计数差异：{outcome.count_repairs!r}"
            )
            assert "body has source=1 target=1" in outcome.count_repairs[0], (
                f"{name}: 提示未给出实际正文计数：{outcome.count_repairs!r}"
            )

    assert not silent, "反斜杠前缀的正文行在补丁被判有效时被静默消费:\n" + "\n".join(silent)


# Purpose: a rejected patch must never write, must stay retryable, and must not be handed
# resource locks. Defect type: side effects or lock acquisition on the rejection path.
# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示（会被接受并真的写入），因此本用例改用
# **真正被拒绝的形状**（缺 '---' 头、二进制元数据）来继续保障「拒绝 → 零写入 + 不申请锁」。
def test_rejected_patch_writes_nothing_and_takes_no_lock(tmp_path: pathlib.Path) -> None:
    rejected_shapes = {
        "missing '---' header": "diff --git a/m.txt b/m.txt\n+++ b/m.txt\n@@ -1,1 +1,1 @@\n-a\n+A",
        "binary metadata": (
            "diff --git a/m.txt b/m.txt\nGIT binary patch\n"
            "--- a/m.txt\n+++ b/m.txt\n@@ -1,1 +1,1 @@\n-a\n+A"
        ),
    }
    (tmp_path / "m.txt").write_bytes(b"a\nb\n")

    for index, (name, patch) in enumerate(rejected_shapes.items(), start=1):
        observation = _execute(tmp_path, patch, 5800 + index)
        resources = FileResourceResolver(tmp_path).resolve("apply_patch", {"patch": patch})

        assert observation.status == "error", name
        assert observation.retryable is True, name
        assert (tmp_path / "m.txt").read_bytes() == b"a\nb\n", name
        assert resources.write_paths == (), name
        assert resources.lock_paths == (), name


# Purpose: a valid patch must still request its write path and lock, so the round-5 rejection
# tightening did not over-reject at the scheduling layer. Defect type: over-rejection.
def test_valid_patch_requests_its_write_path_and_lock(tmp_path: pathlib.Path) -> None:
    patch = _valid_patch("m.txt")

    resources = FileResourceResolver(tmp_path).resolve("apply_patch", {"patch": patch})

    assert resources.write_paths == (tmp_path / "m.txt",)
    assert (tmp_path / "m.txt") in resources.lock_paths


# Purpose: parsing must be pure and idempotent - no input mutation, no caching, identical
# diagnosis across calls. Defect type: state leak between calls.
# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示（不再拒绝）。原「invalid」补丁现在被接受并
# 附带一条重算提示，故断言改为「连续调用给出完全一致的重算提示」，其余契约（不改入参、合法补丁
# 结果稳定）保持不变。
def test_parsing_is_pure_and_idempotent_for_valid_and_invalid_patches() -> None:
    miscounted = _section("m.txt", "@@ -1,2 +1,2 @@", " a", "-b", "+c", " d") + "\n"
    valid = _valid_patch("m.txt")

    miscounted_results = [parse_git_unified_diff_detailed(miscounted) for _ in range(4)]
    valid_results = [parse_git_unified_diff_detailed(valid) for _ in range(4)]

    assert miscounted == _section("m.txt", "@@ -1,2 +1,2 @@", " a", "-b", "+c", " d") + "\n"
    assert valid == _valid_patch("m.txt")
    assert all(outcome.error is None and outcome.operations for outcome in miscounted_results)
    assert len({tuple(outcome.count_repairs) for outcome in miscounted_results}) == 1
    assert len(miscounted_results[0].count_repairs) == 1
    assert all(outcome.error is None and outcome.operations for outcome in valid_results)
    assert all(outcome.count_repairs == [] for outcome in valid_results)
    assert len(
        {tuple(op.file_path for op in outcome.operations) for outcome in valid_results}
    ) == 1


# Purpose: a successful patch must still produce the file-changes display payload and the
# written bytes must match the author's intent for a header/body-consistent hunk. Defect
# type: success path regression (lost display data or wrong bytes).
def test_success_path_emits_display_payload_and_exact_bytes(tmp_path: pathlib.Path) -> None:
    (tmp_path / "m.txt").write_bytes(b"a\nb\nc\n")
    patch = _section("m.txt", "@@ -1,3 +1,3 @@", " a", "-b", "+B", " c") + "\n"

    observation = _execute(tmp_path, patch, 5900)

    assert observation.status == "success", observation.error
    assert observation.display_data["kind"] == "file-changes"
    change = observation.display_data["changes"][0]
    assert change["path"] == "m.txt"
    assert change["status"] == "modified"
    assert (tmp_path / "m.txt").read_bytes() == b"a\nB\nc\n"


# Purpose: applying the same patch twice must be rejected the second time (the first run
# changed the context) and must not corrupt the file. Defect type: non-idempotent apply that
# silently rewrites.
def test_replaying_an_applied_patch_is_rejected_without_corruption(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "m.txt").write_bytes(b"a\n")
    patch = _valid_patch("m.txt")

    first = _execute(tmp_path, patch, 6000)
    second = _execute(tmp_path, patch, 6001)

    assert first.status == "success"
    assert (tmp_path / "m.txt").read_bytes() == b"A\n"
    assert second.status == "error"
    assert (tmp_path / "m.txt").read_bytes() == b"A\n"


# Purpose: an empty or whitespace-only patch must be rejected with the documented
# non-empty requirement, and must not be misreported as a count problem. Defect type:
# error-class misattribution for the degenerate input.
@pytest.mark.parametrize("patch", ["", "   ", "\n\n"])
def test_degenerate_patch_is_rejected_as_non_empty_requirement(patch: str) -> None:
    operations, error = parse_git_unified_diff(patch)

    assert operations == []
    assert error == "patch must be a non-empty Git unified diff"


# Purpose: the ``*** Begin Patch`` prohibition documented in the tool description must be
# enforced, and the failure must be a parse error rather than a silent acceptance.
# Defect type: documented prohibition not enforced.
def test_legacy_patch_markers_are_rejected() -> None:
    wrapped = "*** Begin Patch\n" + _valid_patch() + "*** End Patch\n"

    operations, error = parse_git_unified_diff(wrapped)

    assert operations == []
    assert error is not None
    assert "diff --git" in error


# Purpose: a duplicate section for the same path must be rejected (one update per file per
# patch), keeping the "one existing file per section" contract meaningful. Defect type:
# duplicated target silently applied twice.
def test_duplicate_section_for_the_same_path_is_rejected() -> None:
    patch = (
        _section("m.txt", "@@ -1,1 +1,1 @@", "-a", "+A")
        + "\n"
        + _section("m.txt", "@@ -1,1 +1,1 @@", "-a", "+B")
        + "\n"
    )

    operations, error = parse_git_unified_diff(patch)

    assert operations == []
    assert error is not None
    assert "duplicate diff section" in error


# Purpose: the workspace containment promise must hold at execution time, not only at parse
# time: an escaping path must be rejected with nothing written outside. Defect type: contract
# promise not enforced end to end.
def test_workspace_containment_is_enforced_end_to_end(tmp_path: pathlib.Path) -> None:
    outside_dir = tmp_path.parent / "pt_r5_outside"
    outside_dir.mkdir(exist_ok=True)
    outside = outside_dir / "leak.txt"
    outside.write_bytes(b"before\n")

    observation = _execute(tmp_path, _valid_patch("../pt_r5_outside/leak.txt"), 6100)

    assert observation.status == "error"
    assert outside.read_bytes() == b"before\n"


# ---------------------------------------------------------------------------
# Attack surface 6: the supporting collaborators of the patch link
# (atomic_write / patch_diff / patch_apply / fuzzy_match) as reached from apply_patch
# ---------------------------------------------------------------------------


# Purpose: applying a patch to a CRLF file must not silently LF-ify it - the atomic writer
# preserves the target's existing line ending. Defect type: line-ending destruction.
def test_crlf_target_keeps_its_line_ending_after_apply(tmp_path: pathlib.Path) -> None:
    (tmp_path / "m.txt").write_bytes(b"a\r\nb\r\n")
    patch = _section("m.txt", "@@ -1,2 +1,2 @@", " a", "-b", "+B") + "\n"

    observation = _execute(tmp_path, patch, 6200)

    assert observation.status == "success", observation.error
    assert (tmp_path / "m.txt").read_bytes() == b"a\r\nB\r\n"


# Purpose: a UTF-8 BOM in the target must survive a patch. Defect type: BOM loss / doubling.
def test_bom_target_keeps_exactly_one_bom_after_apply(tmp_path: pathlib.Path) -> None:
    (tmp_path / "m.txt").write_bytes(b"\xef\xbb\xbfa\nb\n")
    patch = _section("m.txt", "@@ -1,2 +1,2 @@", " a", "-b", "+B") + "\n"

    observation = _execute(tmp_path, patch, 6201)

    assert observation.status == "success", observation.error
    written = (tmp_path / "m.txt").read_bytes()
    assert written == b"\xef\xbb\xbfa\nB\n", f"BOM 未按原样保留: {written!r}"


# Purpose: the diff projection in the display payload must be a syntactic Git diff whose
# bodies reproduce the change that the bytes on disk show. Defect type: the payload
# advertising a change that does not match the file.
def test_display_payload_patch_reproduces_the_disk_change(tmp_path: pathlib.Path) -> None:
    (tmp_path / "m.txt").write_bytes(b"one\ntwo\n")
    patch = _section("m.txt", "@@ -1,2 +1,2 @@", "-one", "+ONE", " two") + "\n"

    observation = _execute(tmp_path, patch, 6202)

    assert observation.status == "success", observation.error
    change = observation.display_data["changes"][0]
    on_disk = (tmp_path / "m.txt").read_text(encoding="utf-8")
    assert on_disk == "ONE\ntwo\n"
    projected = change["patch"]
    assert projected.startswith("diff --git a/m.txt b/m.txt")
    body = projected.split("\n@@ ", 1)[1].split("\n", 1)[1]
    added = [line for line in body.splitlines() if line.startswith("+")]
    removed = [line for line in body.splitlines() if line.startswith("-")]
    assert added == ["+ONE"], f"展示 diff 的新增行与实际不符: {projected!r}"
    assert removed == ["-one"], f"展示 diff 的删除行与实际不符: {projected!r}"


# Purpose: the diff statistics surfaced to the client must count the applied lines, not the
# header lines, and must agree with the per-change counters. Defect type: miscounted stats.
def test_diff_stats_count_only_real_changes(tmp_path: pathlib.Path) -> None:
    (tmp_path / "m.txt").write_bytes(b"one\ntwo\nthree\n")
    patch = _section("m.txt", "@@ -1,3 +1,3 @@", "-one", "+ONE", " two", "-three", "+THREE") + "\n"

    observation = _execute(tmp_path, patch, 6203)

    assert observation.status == "success", observation.error
    stats = observation.display_data["diff_stats"]
    assert stats["total_files"] == 1
    assert stats["total_insertions"] == 2, f"新增行计数错误: {stats!r}"
    assert stats["total_deletions"] == 2, f"删除行计数错误: {stats!r}"
    change = observation.display_data["changes"][0]
    per_change = (change["insertions"], change["deletions"])
    assert per_change == (2, 2), f"单文件统计与总统计不一致: {change!r}"


# Purpose: a hunk whose context does not appear must be reported as a not-found hunk, never
# applied approximately. Defect type: fuzzy matching accepting a hunk with no true match.
def test_unmatched_hunk_is_reported_and_nothing_is_written(tmp_path: pathlib.Path) -> None:
    (tmp_path / "m.txt").write_bytes(b"alpha\nbeta\n")
    patch = _section("m.txt", "@@ -1,1 +1,1 @@", "-gamma", "+GAMMA") + "\n"

    observation = _execute(tmp_path, patch, 6204)

    assert observation.status == "error"
    assert "hunk 1 not found" in observation.error
    assert (tmp_path / "m.txt").read_bytes() == b"alpha\nbeta\n"


# Purpose: the write path must refuse to escape its containment root even when the resolver
# was already bypassed, i.e. the atomic writer re-checks before replacing.
# Defect type: missing defence in depth at the write boundary.
def test_atomic_write_refuses_a_path_outside_the_containment_root(
    tmp_path: pathlib.Path,
) -> None:
    from app.core.tools.tool_handler.patch_write.atomic_write import atomic_write_text

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"keep\n")

    with pytest.raises(OSError):
        atomic_write_text(outside, "clobbered\n", containment_root=root)

    assert outside.read_bytes() == b"keep\n"


# Purpose: the atomic writer must leave no temporary file behind when the write fails, and
# must not leave a half-written target. Defect type: temp-file leak / torn write.
def test_atomic_write_leaves_no_temp_file_when_it_fails(tmp_path: pathlib.Path) -> None:
    from app.core.tools.tool_handler.patch_write import atomic_write

    (tmp_path / "m.txt").write_bytes(b"original\n")

    def exploding_fsync(_descriptor: int) -> None:
        raise OSError("simulated fsync failure")

    original_fsync = atomic_write.os.fsync
    atomic_write.os.fsync = exploding_fsync
    try:
        with pytest.raises(OSError):
            atomic_write.atomic_write_text(tmp_path / "m.txt", "new\n")
    finally:
        atomic_write.os.fsync = original_fsync

    assert (tmp_path / "m.txt").read_bytes() == b"original\n"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".tmp_write_")]
    assert leftovers == [], f"失败后残留临时文件: {leftovers}"


# Purpose: the line-number pollution guard must flag content that looks like read_file
# output, so a model cannot round-trip the numbered listing back into a file.
# Defect type: guard too weak to detect the pollution it exists for.
def test_line_number_pollution_guard_flags_numbered_content() -> None:
    from app.core.tools.tool_handler.patch_write.atomic_write import looks_like_line_numbered

    # read_file renders lines as f"{line_number}| {line}" (no padding), so the guard must
    # catch exactly that shape.
    numbered = "1| import os\n2| print(1)\n3| \n"
    clean = "import os\nprint(1)\n"

    assert looks_like_line_numbered(numbered) is True
    assert looks_like_line_numbered(clean) is False
    assert looks_like_line_numbered("") is False
    # Below the threshold it must not fire, so ordinary code with a "1| " literal survives.
    assert looks_like_line_numbered("1| a\nplain\nplain\nplain\nplain\n") is False


# Purpose: the line-ending and BOM detectors must agree with what the writer preserves.
# Defect type: detector / writer disagreement producing format drift.
@pytest.mark.parametrize(
    ("text", "eol"),
    [("a\r\nb\r\n", "\r\n"), ("a\nb\n", "\n"), ("", "\n"), ("a\rb", "\n")],
)
def test_line_ending_detector_matches_the_writer(text: str, eol: str) -> None:
    from app.core.tools.tool_handler.patch_write.atomic_write import detect_line_ending

    assert detect_line_ending(text) == eol


# Purpose: a patch that produces no textual change must be rejected rather than reported as a
# successful modification with an empty diff. Defect type: phantom success.
def test_hunk_without_a_real_change_is_rejected() -> None:
    patch = _section("m.txt", "@@ -1,1 +1,1 @@", " a") + "\n"

    operations, error = parse_git_unified_diff(patch)

    assert operations == []
    assert error is not None
    assert "does not change file contents" in error


# Purpose: a section that parses to zero operations must be rejected explicitly instead of
# yielding an empty operation list that callers would treat as "nothing to do".
# Defect type: empty success.
def test_section_without_operations_is_rejected() -> None:
    operations, error = parse_git_unified_diff("diff --git a/m.txt b/m.txt\n")

    assert operations == []
    assert error is not None


# Purpose: a target that is a directory must be rejected as "not an existing regular file"
# (the contract says only existing text files). Defect type: directory accepted as a target.
def test_directory_target_is_rejected_as_a_regular_file_requirement(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "adir").mkdir()

    observation = _execute(tmp_path, _valid_patch("adir"), 6205)

    assert observation.status == "error"
    assert "not an existing regular file" in observation.error
    assert (tmp_path / "adir").is_dir()


# Purpose: a patch naming two different files in 'a/' and 'b/' is a rename and must be
# rejected before any filesystem work. Defect type: rename slipped through as an update.
def test_a_and_b_path_mismatch_is_rejected() -> None:
    patch = (
        "diff --git a/one.txt b/two.txt\n--- a/one.txt\n+++ b/two.txt\n" "@@ -1,1 +1,1 @@\n-a\n+A\n"
    )

    operations, error = parse_git_unified_diff(patch)

    assert operations == []
    assert error is not None
    assert "same file" in error or "renaming" in error


# Purpose: the C-quoted path decoder must accept a legitimate quoted path and reject a
# malformed one, and the quotes must be removed before resolution.
# Defect type: quoted-path handling leaking quote characters into the resolved path.
def test_quoted_paths_are_decoded_and_malformed_quotes_rejected() -> None:
    good = (
        'diff --git "a/spaced name.txt" "b/spaced name.txt"\n'
        '--- "a/spaced name.txt"\n+++ "b/spaced name.txt"\n'
        "@@ -1,1 +1,1 @@\n-a\n+A\n"
    )
    bad = (
        'diff --git "a/unterminated.txt b/unterminated.txt\n'
        "--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-a\n+A\n"
    )

    operations, error = parse_git_unified_diff(good)
    bad_operations, bad_error = parse_git_unified_diff(bad)

    assert error is None and operations[0].file_path == "spaced name.txt"
    assert bad_operations == [] and bad_error is not None


# ---------------------------------------------------------------------------
# Attack surface 7: the fuzzy matching chain as reached from apply_patch hunks
# ---------------------------------------------------------------------------


# Purpose: a hunk whose context differs from the file only by indentation must still be
# applied (that is the chain's purpose), and the replacement must adopt the file's real
# indentation rather than the model's. Defect type: reindent drift / silent mis-apply.
def test_indentation_only_drift_is_applied_with_the_file_indentation(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "m.txt").write_bytes(b"def f():\n    return 1\n")
    # Model writes the hunk without the file's 4-space indent.
    patch = _section("m.txt", "@@ -2,1 +2,1 @@", "-return 1", "+return 2") + "\n"

    observation = _execute(tmp_path, patch, 6300)

    assert observation.status == "success", observation.error
    assert (tmp_path / "m.txt").read_bytes() == b"def f():\n    return 2\n"


# Purpose: an ambiguous context (two identical occurrences) must be refused rather than
# applied to an arbitrary one. Defect type: non-unique fuzzy match silently applied.
def test_ambiguous_context_is_refused_rather_than_guessed(tmp_path: pathlib.Path) -> None:
    (tmp_path / "m.txt").write_bytes(b"x = 1\ny = 2\nx = 1\ny = 2\n")
    patch = _section("m.txt", "@@ -1,2 +1,2 @@", "-x = 1", "+x = 9", " y = 2") + "\n"

    observation = _execute(tmp_path, patch, 6301)

    # Exact matching would find the block twice, so the unique-match requirement applies.
    assert (
        observation.status == "error"
    ), f"非唯一上下文被随意应用: {(tmp_path / 'm.txt').read_bytes()!r}"
    assert (tmp_path / "m.txt").read_bytes() == b"x = 1\ny = 2\nx = 1\ny = 2\n"


# Purpose: a smart-quote / em-dash drift in the model's hunk must be normalized onto the
# file's real characters while untouched text keeps its original characters.
# Defect type: Unicode normalization corrupting untouched text.
def test_unicode_drift_matches_but_preserves_untouched_characters(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "m.txt").write_text("say \u201chello\u201d\nkeep\n", encoding="utf-8")
    patch = _section("m.txt", "@@ -1,2 +1,2 @@", '-say "hello"', "+say GOODBYE", " keep") + "\n"

    observation = _execute(tmp_path, patch, 6302)

    assert observation.status == "success", observation.error
    written = (tmp_path / "m.txt").read_text(encoding="utf-8")
    assert written == "say GOODBYE\nkeep\n", f"Unicode 漂移处理结果不符: {written!r}"


# Purpose: the escape-drift guard in ``fuzzy_match._detect_escape_drift`` keys on the literal
# sequence being present in BOTH old and new. A hunk whose ``-`` lines are clean and whose
# ``+`` line carries a spurious '\\'' is therefore not detected as drift and the backslash is
# written into the file while the call reports success.
# Defect type: escape drift written to disk (guard precondition too narrow).
@pytest.mark.xfail(
    strict=True,
    reason="已知应用层缺口（非本次解析层改动引入）：转义漂移守卫仅在 - / + 双侧都含 '\\'' 时触发",
)
def test_escape_drift_only_in_the_added_line_is_refused(tmp_path: pathlib.Path) -> None:
    (tmp_path / "m.txt").write_bytes(b"value = 'x'\n")
    patch = _section("m.txt", "@@ -1,1 +1,1 @@", "-value = 'x'", "+value = \\'y\\'") + "\n"

    status, written = _tool_bytes(tmp_path, patch, "value = 'x'\n", 6303)

    drift_written = status == "success" and b"\\'" in written
    assert not drift_written, (
        "转义漂移（仅出现在 '+' 行）被原样写入文件并报成功：" f"status={status} wrote={written!r}"
    )


# Purpose: document the guard's real precondition so a future change is noticed: the guard
# fires only when the literal sequence is present in *both* old and new text.
# Defect type: guard precondition narrower than the drift it must catch.
def test_escape_drift_guard_fires_when_both_sides_carry_the_escape() -> None:
    from app.core.tools.tool_handler.patch_write.fuzzy_match import fuzzy_find_and_replace

    content = "x = 'a'\n"
    escaped = "x = \\'a\\'"
    replacement = "x = \\'b\\'"

    _, count, _, error = fuzzy_find_and_replace(content, escaped, replacement)

    assert count == 0 and error is not None
    assert "Escape-drift detected" in error


# Purpose: applying a hunk whose context is present but whose declared body also contains an
# insertion-only hunk must place the insertion at the declared coordinate.
# Defect type: off-by-one insertion placement.
def test_insertion_only_hunk_lands_at_the_declared_coordinate(tmp_path: pathlib.Path) -> None:
    (tmp_path / "m.txt").write_bytes(b"a\nb\nc\nd\n")
    patch = _section("m.txt", "@@ -2,0 +3,1 @@", "+INSERTED") + "\n"

    observation = _execute(tmp_path, patch, 6304)

    assert observation.status == "success", observation.error
    assert (tmp_path / "m.txt").read_bytes() == b"a\nb\nINSERTED\nc\nd\n"


# Purpose: the file-with-trailing-newline case must keep its trailing newline, and an explicit
# '\\ No newline' marker on the target must drop it. Defect type: trailing-newline drift.
@pytest.mark.parametrize(
    ("before", "body", "expected"),
    [
        ("a\n", ("-a", "+A"), b"A\n"),
        ("a\n", ("-a", "+A", "\\ No newline at end of file"), b"A"),
    ],
)
def test_trailing_newline_state_is_preserved(
    tmp_path: pathlib.Path, before: str, body: tuple[str, ...], expected: bytes
) -> None:
    source = sum(1 for line in body if line[:1] in {" ", "-"})
    target = sum(1 for line in body if line[:1] in {" ", "+"})
    patch = _section("m.txt", f"@@ -1,{source} +1,{target} @@", *body) + "\n"

    status, written = _tool_bytes(tmp_path, patch, before, 6305)

    assert status == "success", f"应当被应用，实际 status={status}"
    assert written == expected, f"尾换行状态漂移: {written!r} != {expected!r}"


# Purpose: a source file WITHOUT a trailing newline replaced by a hunk that carries no
# '\\ No newline' marker. Real Git refuses this patch ("patch does not apply" - verified with
# git 2.x in a scratch repo: source-side marker required for a markerless-body match), while
# this tool accepts it and silently appends a trailing newline that the author never wrote.
# Defect type: trailing-newline drift / accepting a patch Git rejects.
@pytest.mark.xfail(
    strict=True,
    reason="已知应用层缺口（非本次解析层改动引入）：源文件无尾换行且补丁无标记时仍会补上换行",
)
def test_markerless_hunk_on_a_no_newline_file_is_rejected(tmp_path: pathlib.Path) -> None:
    (tmp_path / "m.txt").write_bytes(b"a")
    patch = _section("m.txt", "@@ -1,1 +1,1 @@", "-a", "+A") + "\n"

    status, written = _tool_bytes(tmp_path, patch, "a", 6307)

    if status == "success":
        assert written == b"A", (
            "源文件无尾换行且补丁无标记，工具报成功却给文件补上了作者未写的尾换行；"
            f"真实 git apply 对该补丁报 'patch does not apply'。wrote={written!r}"
        )


# Purpose: the write-back must be atomic - either the file keeps its old content or it has the
# full new content, never a mix. Verified by observing the file through a failing rename.
# Defect type: torn write leaving a partially updated file.
def test_failed_replace_leaves_the_original_file_intact(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    from app.core.tools.tool_handler.patch_write import atomic_write

    (tmp_path / "m.txt").write_bytes(b"a\nb\n")
    patch = _section("m.txt", "@@ -1,2 +1,2 @@", "-a", "+A", " b") + "\n"

    def exploding_replace(_source, _destination) -> None:
        raise OSError("simulated replace failure")

    original_replace = atomic_write.os.replace
    monkeypatch.setattr(atomic_write.os, "replace", exploding_replace)
    try:
        observation = _execute(tmp_path, patch, 6306)
    finally:
        monkeypatch.setattr(atomic_write.os, "replace", original_replace)

    assert observation.status == "error", "替换失败必须报告为错误而不是成功"
    assert (tmp_path / "m.txt").read_bytes() == b"a\nb\n", "替换失败后文件被破坏"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".tmp_write_")]
    assert leftovers == [], f"替换失败后残留临时文件: {leftovers}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
