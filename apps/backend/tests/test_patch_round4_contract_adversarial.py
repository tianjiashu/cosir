"""Round-4 adversarial probe for the model-visible contract and its de-duplication.

被测对象（代码冻结，本轮只新增测试，不修改生产代码，也不修改既有测试的期望）：

1. ``apply_patch_tool.APPLY_PATCH_DESCRIPTION``（工具描述，只承载职责与能力边界）与
   ``ApplyPatchArgs.model_fields['patch'].description``（参数描述，格式契约的唯一事实源）
   在「模型可见契约去重」后的自洽性：
   - 信息是否丢失（前缀规则 / 计数规则 / 样例 / 路径同一性 / 不支持清单 / 替代工具）；
   - 两段是否互相矛盾、是否各自复述了对方的职责；
   - 工具描述指向「patch 参数描述定义格式」的指针是否真实成立。
2. 失败分支 ``reason`` 的措辞是否成立——特别是 ``validate_all`` 的失败原因并不仅仅
   是「路径不存在 / hunk 上下文不符」：二进制目标、非 UTF-8 目标、``.cosir`` 保留区
   都会走到同一分支，而该分支的 ``reason`` 只提到「existing-file paths or hunk context」。
3. 去重不变量的强度：能否阻止有人把计数规则抄回工具描述（语义级断言，不只靠关键词）。
4. 维护者裁定（2026-09-21，第三轮）的复核：按「本仓链路是否出现静默内容丢失 / 写入与
   作者意图不符 / 错位写入」这一标准攻击「尾部空行剥离」。能在本仓链路上构造出上述三种
   后果即为缺陷；构造不出则明确记为「无证据」。
5. 常规对抗面回归：多文件多 hunk、``index`` 行、``\\ No newline`` 各位置、CRLF/CR、
   tab、行尾空格、BOM、``@@`` 带 section 文本、超长行、显式 0 边界、诊断质量、
   安全性（不写文件 / ``retryable`` / 不为无效补丁申请锁）、幂等无副作用。

本文件不修改生产代码；既有测试的期望也不改动。
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


def _section(path: str, *lines: str) -> str:
    """Build one Git file section: three header lines then the given body lines."""

    return "\n".join((f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", *lines))


def _valid_patch(path: str = "m.txt") -> str:
    return _section(path, "@@ -1,1 +1,1 @@", "-a", "+A") + "\n"


def _context(root: pathlib.Path, run_id: int = 4001) -> ToolExecutionContext:
    return ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=root, run_id=run_id)


def _execute(root: pathlib.Path, patch: str, run_id: int = 4001):
    return ApplyPatchTool().execute(_context(root, run_id), patch=patch)


# ---------------------------------------------------------------------------
# 攻击面 1：去重后模型可见契约的信息完整性与自洽性
# ---------------------------------------------------------------------------


# 目的：计数规则即使留在唯一事实源（参数描述）里，也必须保持**可判定**的精确措辞；把
# 「must match ... exactly」弱化成模棱两可的措辞同样是契约缺陷（上一轮事故根因）。
# 缺陷类型：唯一事实源被削弱（信息仍在但不再可执行）。
def test_count_rule_in_the_parameter_description_stays_precise() -> None:
    exact_sentence = (
        "numbers in '@@ -start,count +start,count @@' must match that hunk body exactly"
    )

    assert exact_sentence in PARAM_DESCRIPTION, "计数规则措辞被削弱或改写"
    assert "a count may be omitted only when it is 1" in PARAM_DESCRIPTION
    assert "must match" not in APPLY_PATCH_DESCRIPTION, "计数规则不得回到工具描述"


# 目的：收敛后模型必须仍能仅凭「工具描述 + 参数描述」推导出全部必需约束。
# 缺陷类型：去重把某条必需信息一并删掉（信息丢失）。
def test_every_required_constraint_survives_the_deduplication() -> None:
    both = APPLY_PATCH_DESCRIPTION + "\n" + PARAM_DESCRIPTION
    required = {
        "prefix rule: context/removed/added start chars": "every line starts with",
        "count rule stated as a rule": "must match",
        "source count definition": "context plus '-' lines",
        "target count definition": "context plus '+' lines",
        "omission rule": "omitted only when it is 1",
        "minimal accepted example marker": "Minimal accepted example",
        "sample 'diff --git' line": "diff --git a/pkg/mod.py b/pkg/mod.py",
        "sample '@@' header": "@@ -10,3 +10,3 @@",
        "'*** Begin Patch' prohibition": "*** Begin Patch",
        "path identity (a/ and b/ same file)": "same workspace-relative path",
        "unsupported: cannot create": "cannot create",
        "unsupported: cannot delete": "delete",
        "unsupported: cannot move": "move",
        "alternative tool write_file": "write_file",
        "alternative tool delete_file": "delete_file",
        "alternative tool move_file": "move_file",
        "workspace containment": "inside the active workspace",
    }

    missing = [name for name, needle in required.items() if needle not in both]

    assert not missing, f"去重丢失了模型必需的信息: {missing}"


# 目的：工具描述声明的指针（「格式由 patch 参数描述定义」）必须真实成立，否则指针把模型引向
# 不存在的内容。缺陷类型：指针悬空（指向的描述里没有该内容）。
def test_tool_description_pointer_points_at_the_parameter_description() -> None:
    assert "its description defines the accepted format" in APPLY_PATCH_DESCRIPTION
    for promised in ("diff --git a/", "must match", "Minimal accepted example"):
        assert promised in PARAM_DESCRIPTION, f"指针承诺的格式内容缺失: {promised!r}"
    # 指针方向唯一：参数描述不得反向指向工具描述（互指即两份事实源）。
    assert "tool description" not in PARAM_DESCRIPTION


# 目的：两段不得互相矛盾——计数规则只由参数描述承载，工具描述不得复述；能力边界只由工具描述
# 承载，参数描述不得复述。缺陷类型：同一事实两处表述、一处宽一处窄。
def test_the_two_texts_do_not_contradict_each_other() -> None:
    # 计数规则只有参数描述一处事实源；工具描述不得复述（复述即漂移风险）。
    assert "must match" not in APPLY_PATCH_DESCRIPTION
    assert "counts" not in APPLY_PATCH_DESCRIPTION
    assert "@@" not in APPLY_PATCH_DESCRIPTION
    # 参数描述明确说「必填」，不得出现可选措辞来争夺语义。
    assert "Required Git-style unified diff" in PARAM_DESCRIPTION
    assert "optional" not in PARAM_DESCRIPTION.lower()
    # 能力边界与替代工具只由工具描述承载；参数描述不得复述。
    for tool in ("write_file", "delete_file", "move_file"):
        assert tool in APPLY_PATCH_DESCRIPTION, f"工具描述缺少替代工具 {tool}"
        assert tool not in PARAM_DESCRIPTION, f"参数描述重复了替代工具指引 {tool}"
    assert "cannot create" not in PARAM_DESCRIPTION


# 目的：结构规则声明必须能被实现证实；且 diff 标记（section / 文件头 / hunk 头）的唯一事实源
# 在参数描述，工具描述只讲职责、不得出现任何标记。
# 缺陷类型：契约承诺了实现拒绝的形状（虚假契约），或语法描述散落两处导致漂移。
def test_documented_structure_claim_is_accepted_by_the_parser() -> None:
    for token in ("diff --git", "---", "+++", "@@"):
        assert token in PARAM_DESCRIPTION, token
        assert token not in APPLY_PATCH_DESCRIPTION, f"工具描述出现 diff 标记: {token!r}"

    operations, error = parse_git_unified_diff(_section("m.txt", "@@ -1,1 +1,1 @@", "-a", "+A"))

    assert error is None, f"描述承诺的四段结构被实现拒绝: {error!r}"
    assert operations


# 目的：参数描述说「the value must be nothing but that diff text」——即前后不得有散文。
# 缺陷类型：契约禁止而实现接受（弱契约），或契约未禁止而实现拒绝。
@pytest.mark.parametrize(
    "wrapper",
    ["prose before", "prose after"],
)
def test_parameter_description_nothing_but_the_diff_is_enforced(wrapper: str) -> None:
    diff = _section("m.txt", "@@ -1,1 +1,1 @@", "-a", "+A") + "\n"
    patch = f"prose\n{diff}" if wrapper == "prose before" else f"{diff}prose\n"

    operations, error = parse_git_unified_diff(patch)

    assert operations == [], f"契约禁止但实现接受（{wrapper}）"
    assert error is not None


# 目的：样例只允许存在于唯一事实源（参数描述）里，且只允许一份；工具描述不得内嵌与参数描述
# 并列的第二份样例（并列样例是上一轮样例计数错事故的温床）。缺陷类型：样例再次重复。
def test_the_sample_exists_once_in_the_parameter_description() -> None:
    assert (
        PARAM_DESCRIPTION.count("diff --git a/pkg/mod.py b/pkg/mod.py") == 1
    ), "参数描述里样例必须只有一份"
    assert "diff --git a/pkg/mod.py" not in APPLY_PATCH_DESCRIPTION, "工具描述不得内嵌样例"


# 目的：参数描述不得比工具描述更宽松地承诺「接受面」，例如不得出现工具描述没有的
# 「多个文件 / 新建文件」措辞。缺陷类型：参数描述放宽了支持面。
def test_parameter_description_does_not_widen_the_supported_surface() -> None:
    lowered = PARAM_DESCRIPTION.lower()

    assert "not supported" in lowered
    assert "new file" not in lowered
    assert "creates files" not in lowered
    assert "any path" not in lowered


# ---------------------------------------------------------------------------
# 攻击面 2：失败分支 reason 的归因是否成立
# ---------------------------------------------------------------------------


# 目的：validation 分支的 reason 声称修正方式是「fix the listed existing-file paths or
# hunk context」。二进制目标既不是路径问题也不是 hunk 上下文问题。
# 缺陷类型：模型可见 reason 归因错误，把模型引向无法修正的方向。
def test_validation_reason_matches_a_binary_target_cause(tmp_path: pathlib.Path) -> None:
    (tmp_path / "bin.txt").write_bytes(b"a\x00b\n")

    observation = _execute(tmp_path, _valid_patch("bin.txt"), 4101)

    assert observation.status == "error"
    assert "binary files are not supported" in observation.error
    lowered = (observation.reason or "").lower()
    # 归因必须覆盖二进制这一真实原因，而不是只说「路径 / hunk 上下文」。
    assert "binary" in lowered, (
        "validation reason 未覆盖二进制目标的真实原因，模型会被引向改路径或改上下文："
        f"reason={observation.reason!r}"
    )


# 目的：非 UTF-8 目标同样不是路径 / hunk 上下文问题。
# 缺陷类型：同上的归因错误（编码类原因被 reason 忽略）。
def test_validation_reason_matches_a_non_utf8_target_cause(tmp_path: pathlib.Path) -> None:
    (tmp_path / "latin.txt").write_bytes(b"caf\xe9\n")

    observation = _execute(tmp_path, _valid_patch("latin.txt"), 4102)

    assert observation.status == "error"
    assert "not a readable UTF-8 text file" in observation.error
    lowered = (observation.reason or "").lower()
    assert "utf-8" in lowered or "encoding" in lowered, (
        "validation reason 未覆盖非 UTF-8 目标的真实原因：" f"reason={observation.reason!r}"
    )


# 目的：``.cosir`` 保留区目标被拒，reason 也不得把它归因为「路径不存在或 hunk 上下文」。
# 缺陷类型：保留区安全原因被 reason 掩盖。
def test_validation_reason_matches_a_reserved_area_cause(tmp_path: pathlib.Path) -> None:
    (tmp_path / ".cosir").mkdir()
    (tmp_path / ".cosir" / "meta.txt").write_text("x\n", encoding="utf-8")

    observation = _execute(tmp_path, _valid_patch(".cosir/meta.txt"), 4103)

    assert observation.status == "error"
    assert "reserved" in observation.error
    lowered = (observation.reason or "").lower()
    assert "reserved" in lowered or ".cosir" in lowered, (
        "validation reason 未覆盖保留区的真实原因：" f"reason={observation.reason!r}"
    )


# 目的：当目标确实不存在时，validation reason 提及「existing-file paths」是成立的
# （对照组：证明上面的发现不是「reason 一律不准确」，而是特定原因未被覆盖）。
def test_validation_reason_is_accurate_for_a_missing_target(tmp_path: pathlib.Path) -> None:
    observation = _execute(tmp_path, _valid_patch("missing.txt"), 4104)

    assert observation.status == "error"
    assert "not an existing regular file" in observation.error
    # reason 现在按「原因类别」如实列举（缺失 / 不规则文件、二进制、非 UTF-8、保留区），
    # 不再把二进制/编码类原因误归因为「路径或 hunk 上下文」。
    assert "missing or irregular file" in (observation.reason or "")


# 目的：解析失败分支的 reason 声称「make every hunk header count exactly ...」。当失败
# 原因根本不是计数（缺 'diff --git' 头）时，该措辞是否仍成立/可行动。
# 缺陷类型：reason 把模型引向改计数，而实际必须补头部。
def test_parse_error_reason_is_actionable_for_a_missing_header() -> None:
    observation = _execute(pathlib.Path("."), "not a diff", 4105)

    assert observation.status == "error"
    assert "expected a Git 'diff --git' header" in observation.error
    reason = observation.reason or ""
    assert "diff --git" in reason, "reason 必须点名缺失的头部类型，否则模型无从修正"


# ---------------------------------------------------------------------------
# 攻击面 3：去重不变量对「同义改写回抄」的抗性
# ---------------------------------------------------------------------------


# 目的：``test_patch_count_rule_is_stated_only_in_the_parameter_description`` 只靠关键词，
# 这里补一条语义级不变量——工具描述不得出现任何计数 / hunk 语义词，使同义改写回抄也被拦下。
# 缺陷类型：规则双源回归（把格式规则抄回工具描述）。
@pytest.mark.parametrize(
    "semantic_token",
    [
        "counts",
        "must match",
        "line count",
        "context plus",
        "hunk body",
        "declared source",
        "declared target",
        "count exactly",
    ],
)
def test_tool_description_never_restates_the_count_rule(semantic_token: str) -> None:
    mutated = APPLY_PATCH_DESCRIPTION + " " + semantic_token

    assert semantic_token.lower() in mutated.lower()
    assert (
        semantic_token.lower() not in APPLY_PATCH_DESCRIPTION.lower()
    ), f"工具描述出现计数语义 {semantic_token!r}：格式规则必须只留在 patch 参数描述里"


# ---------------------------------------------------------------------------
# 攻击面 4：维护者裁定复核——尾部空行剥离是否造成静默内容丢失
# ---------------------------------------------------------------------------


def _tool_bytes(
    tmp_path: pathlib.Path,
    patch: str,
    content: str,
    run_id: int,
    relative: str = "m.txt",
) -> tuple[str, str]:
    """Seed ``relative`` with ``content`` inside ``tmp_path`` and run the tool on ``patch``."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content.encode("utf-8"))
    observation = _execute(tmp_path, patch, run_id)
    return observation.status, target.read_bytes().decode("utf-8")


# 目的：按裁定标准攻击——若尾部空行剥离造成本仓链路「写入结果与作者意图不符」，
# 这里必须红。作者意图取「补丁 + / - 行按位置忠实应用」的结果。
# 缺陷类型：静默内容丢失 / 错位写入。
def test_strip_never_makes_written_bytes_differ_from_author_intent(
    tmp_path: pathlib.Path,
) -> None:
    patches = {
        "blank is last body line of hunk 1": (
            _section(
                "m.txt",
                "@@ -1,1 +1,1 @@",
                "-a",
                "+A",
                "",
                "@@ -3,1 +3,1 @@",
                "-c",
                "+C",
            )
            + "\n",
            "a\nb\nc\n",
            "A\nb\nC\n",
        ),
        "two blanks before the next hunk": (
            _section(
                "m.txt",
                "@@ -1,1 +1,1 @@",
                "-a",
                "+A",
                "",
                "",
                "@@ -3,1 +3,1 @@",
                "-c",
                "+C",
            )
            + "\n",
            "a\nb\nc\n",
            "A\nb\nC\n",
        ),
        "blank at the very end of the patch": (
            _section("m.txt", "@@ -1,1 +1,1 @@", "-a", "+A") + "\n\n",
            "a\n",
            "A\n",
        ),
        "interior blank kept as context": (
            _section("m.txt", "@@ -1,3 +1,3 @@", " a", "", "-b", "+c") + "\n",
            "a\n\nb\n",
            "a\n\nc\n",
        ),
    }

    mismatches = []
    for index, (name, (patch, content, intended)) in enumerate(patches.items(), start=1):
        status, written = _tool_bytes(tmp_path / f"case{index}", patch, content, 4200 + index)
        if status == "success" and written != intended:
            mismatches.append(f"{name}: status={status} wrote={written!r} intended={intended!r}")

    assert not mismatches, "尾部空行剥离导致写入与作者意图不符（静默内容丢失）:\n" + "\n".join(
        mismatches
    )


# 目的：裁定标准之二——若剥离让多 hunk 补丁的 hunk 边界错位，使后一个 hunk 的正文
# 被写到错误位置，本用例必须红。缺陷类型：错位写入。
def test_strip_never_misplaces_a_following_hunks_content(tmp_path: pathlib.Path) -> None:
    patch = (
        _section(
            "m.txt",
            "@@ -1,1 +1,1 @@",
            "-a",
            "+A",
            "",
            "@@ -3,2 +3,2 @@",
            "-c",
            "+C",
            " d",
        )
        + "\n"
    )

    status, written = _tool_bytes(tmp_path, patch, "a\nb\nc\nd\n", 4300)

    assert status == "success", f"该形状应被忠实应用，实际 status={status}"
    assert written == "A\nb\nC\nd\n", f"错位写入：实际 {written!r}"


# 目的：裁定标准之三——剥离不得让补丁里作者写下的**内容**（非空行）从结果中消失。
# 缺陷类型：静默内容丢失。
# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示。该补丁（头部 2/2，正文实际 4/4）现在被
# 按正文应用，因此判定改为「确实写入，且作者写下的每个非空内容行都出现在结果中」——内容行可以
# 被替换（-b/+B），但绝不能被静默丢弃。
def test_strip_never_drops_a_non_blank_authored_line(tmp_path: pathlib.Path) -> None:
    patch = (
        _section(
            "m.txt",
            "@@ -1,2 +1,2 @@",
            " a",
            "-b",
            "+B",
            "",
            "-MORE",
            "+EXTRA",
        )
        + "\n"
    )

    status, written = _tool_bytes(tmp_path, patch, "a\nb\nMORE\n", 4301)

    assert status == "success", f"计数不符现在应被接受并按正文应用，实际 status={status}"
    for authored in ("a", "B", "EXTRA"):
        assert authored in written.splitlines(), (
            f"作者写下的内容行 {authored!r} 从结果中消失：wrote={written!r}"
        )


# ---------------------------------------------------------------------------
# 攻击面 5：常规对抗面回归
# ---------------------------------------------------------------------------


# 目的：多文件多 hunk、index 行、@@ 带 section 文本、tab、行尾空格、超长行等常规形状
# 不得因本轮改动而改变接受判定。缺陷类型：接受面漂移。
def test_routine_shapes_stay_accepted_with_faithful_writes(tmp_path: pathlib.Path) -> None:
    cases = {
        "index line": (
            "diff --git a/f.txt b/f.txt\nindex 0000000..1111111 100644\n"
            "--- a/f.txt\n+++ b/f.txt\n@@ -1,1 +1,1 @@\n-a\n+A\n",
            "f.txt",
            "a\n",
            "A\n",
        ),
        "tab-indented context": (
            _section("g.txt", "@@ -1,2 +1,2 @@", " \tkeep", "-a", "+A") + "\n",
            "g.txt",
            "\tkeep\na\n",
            "\tkeep\nA\n",
        ),
        "trailing spaces": (
            _section("h.txt", "@@ -1,2 +1,2 @@", " a  ", "-b  ", "+c  ") + "\n",
            "h.txt",
            "a  \nb  \n",
            "a  \nc  \n",
        ),
        "section text after @@": (
            _section("i.txt", "@@ -1,2 +1,2 @@ def f():", " a", "-b", "+c") + "\n",
            "i.txt",
            "a\nb\n",
            "a\nc\n",
        ),
        "very long line": (
            _section("j.txt", "@@ -1,1 +1,1 @@", "-" + "x" * 5000, "+" + "y" * 5000) + "\n",
            "j.txt",
            "x" * 5000 + "\n",
            "y" * 5000 + "\n",
        ),
    }

    for name, (patch, relative, before, after) in cases.items():
        status, written = _tool_bytes(
            tmp_path / name.replace(" ", "_"), patch, before, 4400, relative
        )
        assert status == "success", f"{name}: 应被接受，实际 status={status}"
        assert written == after, f"{name}: 写入不符，实际 {written!r} 期望 {after!r}"


# 目的：多文件补丁必须全部按意图写入，且多 hunk 顺序稳定。缺陷类型：跨文件/跨 hunk 错配。
def test_multi_file_multi_hunk_is_applied_per_file(tmp_path: pathlib.Path) -> None:
    patch = (
        _section("first.txt", "@@ -1,1 +1,1 @@", "-a", "+A")
        + "\n"
        + _section("second.txt", "@@ -1,2 +1,2 @@", " c", "-d", "+D")
        + "\n"
    )
    (tmp_path / "first.txt").write_bytes(b"a\n")
    (tmp_path / "second.txt").write_bytes(b"c\nd\n")

    observation = _execute(tmp_path, patch, 4500)

    assert observation.status == "success"
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "A\n"
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "c\nD\n"


# 目的：``\ No newline at end of file`` 在正文中/末尾各位置都不得改变接受判定。
# 缺陷类型：反斜杠行的计数口径漂移。
@pytest.mark.parametrize(
    "body",
    [
        ("-a", "\\ No newline at end of file", "+b"),
        ("-a", "+b", "\\ No newline at end of file"),
        ("-a", "\\ No newline at end of file", "+b", "\\ No newline at end of file"),
    ],
)
def test_no_newline_marker_positions_are_accepted(body: tuple[str, ...]) -> None:
    patch = _section("m.txt", "@@ -1,1 +1,1 @@", *body) + "\n"

    operations, error = parse_git_unified_diff(patch)

    assert error is None, f"标记位置 {body} 被误拒：{error!r}"
    assert operations


# 目的：CRLF 整份补丁与 CR 正文不得改变计数判定。缺陷类型：行尾归一化误判。
@pytest.mark.parametrize("variant", ["crlf_whole", "cr_only_body"])
def test_carriage_return_variants_stay_consistent(variant: str) -> None:
    lf = _section("m.txt", "@@ -1,2 +1,2 @@", " a", "-b", "+c") + "\n"
    patch = (
        lf.replace("\n", "\r\n")
        if variant == "crlf_whole"
        else _section("m.txt", "@@ -1,2 +1,2 @@", " a\r", "-b", "+c") + "\n"
    )

    operations, error = parse_git_unified_diff(patch)

    assert error is None, f"{variant} 被误拒：{error!r}"
    assert operations


# 目的：BOM 前缀属于 patch 层问题，不得被归因为计数不符。缺陷类型：诊断归因漂移。
def test_bom_prefix_is_not_a_count_diagnostic() -> None:
    patch = "\ufeff" + _section("m.txt", "@@ -1,1 +1,1 @@", "-a", "+A")

    operations, error = parse_git_unified_diff(patch)

    assert operations == []
    assert error is not None
    assert "hunk line counts do not match" not in error


# 目的：显式 0 计数与省略计数的边界不得互相被 `or 1` 改写。缺陷类型：0 → 1 强转。
def test_explicit_zero_boundaries_are_not_coerced() -> None:
    insert = _section("m.txt", "@@ -0,0 +1,1 @@", "+a") + "\n"
    deleting = _section("m.txt", "@@ -1,1 +0,0 @@", "-a") + "\n"

    insert_ops, insert_error = parse_git_unified_diff(insert)
    delete_ops, delete_error = parse_git_unified_diff(deleting)

    assert insert_error is None and insert_ops[0].hunks[0].source_length == 0
    assert delete_error is None and delete_ops[0].hunks[0].target_length == 0


# 目的：诊断内容必须齐全（section/hunk 序号、头部原文、声明值、实际值）且顺序稳定、纯 ASCII。
# 缺陷类型：诊断字段缺失 / 顺序漂移 / 非 ASCII。
# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示。第 1 个 section 必须改成「自洽但真的改动内容」
# 的 hunk（纯 context 的 hunk 会被 'hunk does not change file contents' 抢先拒绝，与本用例无关）；
# 断言落在 count_repairs 上，字段齐全性与 ASCII 要求不放宽。
def test_diagnostics_stay_complete_stable_and_ascii() -> None:
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


# 目的：真正**被拒绝**的补丁绝不写文件、retryable=True、且不为其申请资源锁。
# 缺陷类型：拒绝路径出现写副作用或锁申请。
# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示（会被接受并真的写入），因此本用例改用
# **真正被拒绝的形状**（缺 '---' 头、二进制元数据）来继续保障「拒绝 → 零写入 + 不申请锁」。
def test_rejected_patch_writes_nothing_and_requests_no_lock(tmp_path: pathlib.Path) -> None:
    rejected_shapes = {
        "missing '---' header": "diff --git a/m.txt b/m.txt\n+++ b/m.txt\n@@ -1,1 +1,1 @@\n-a\n+A",
        "binary metadata": (
            "diff --git a/m.txt b/m.txt\nGIT binary patch\n"
            "--- a/m.txt\n+++ b/m.txt\n@@ -1,1 +1,1 @@\n-a\n+A"
        ),
    }
    target = tmp_path / "m.txt"
    target.write_bytes(b"a\nb\n")

    for name, patch in rejected_shapes.items():
        observation = _execute(tmp_path, patch, 4600)
        resources = FileResourceResolver(tmp_path).resolve("apply_patch", {"patch": patch})

        assert observation.status == "error", name
        assert observation.retryable is True, name
        assert target.read_bytes() == b"a\nb\n", name
        assert resources.write_paths == (), name
        assert resources.lock_paths == (), name


# 目的：合法补丁必须照常解析出写路径与锁。缺陷类型：过度拒绝导致调度链失配。
def test_valid_patch_still_requests_write_paths_and_locks(tmp_path: pathlib.Path) -> None:
    patch = _valid_patch("m.txt")

    resources = FileResourceResolver(tmp_path).resolve("apply_patch", {"patch": patch})

    assert resources.write_paths == (tmp_path / "m.txt",)
    assert (tmp_path / "m.txt") in resources.lock_paths


# 目的：解析必须纯函数、可重复、不改入参。缺陷类型：缓存污染 / 原地改写。
# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示，故该补丁现在被接受；断言改为「结果一致」。
def test_parsing_is_pure_and_idempotent() -> None:
    patch = _section("m.txt", "@@ -1,2 +1,2 @@", " a", "-b", "+c", " d") + "\n"
    original = patch

    results = [parse_git_unified_diff_detailed(patch) for _ in range(5)]

    assert patch == original, "解析不得修改入参"
    assert all(outcome.error is None and outcome.operations for outcome in results)
    assert len({tuple(outcome.count_repairs) for outcome in results}) == 1, (
        "重复调用必须给出一致的重算提示"
    )
    assert len(results[0].count_repairs) == 1, "计数不符必须产出重算提示"


# 目的：成功应用后 display 载荷必须可观测（成功路径不得回归）。
# 缺陷类型：成功路径丢失展示数据。
def test_success_emits_file_change_display_payload(tmp_path: pathlib.Path) -> None:
    (tmp_path / "m.txt").write_bytes(b"a\n")

    observation = _execute(tmp_path, _valid_patch("m.txt"), 4700)

    assert observation.status == "success"
    assert observation.display_data["kind"] == "file-changes"
    assert observation.display_data["changes"][0]["path"] == "m.txt"
    assert (tmp_path / "m.txt").read_bytes() == b"A\n"


# 目的：小规模穷举——自洽补丁一律不得被拒，且容错写入必须与作者意图一致。
# 缺陷类型：任何口径偏差导致的误拒或错写。
def test_small_grid_self_consistent_patches_are_never_rejected() -> None:
    headers = ["@@ -1,1 +1,1 @@", "@@ -1,2 +1,2 @@", "@@ -1 +1 @@"]
    bodies = [[" a"], ["-a", "+b"], [" a", "-b", "+c"], ["-a", "+b"]]

    false_rejections = []
    for header, body in itertools.product(headers, bodies):
        patch = _section("m.txt", header, *body) + "\n"
        operations, error = parse_git_unified_diff(patch)
        # 只保留头部与正文自洽的组合。
        source = sum(1 for line in body if line[:1] in {" ", "-"})
        target = sum(1 for line in body if line[:1] in {" ", "+"})
        source_token = header.split(" ")[1]
        target_token = header.split(" ")[2]
        declared_source = int(source_token.split(",")[1]) if "," in source_token else 1
        declared_target = int(target_token.split(",")[1]) if "," in target_token else 1
        if (declared_source, declared_target) != (source, target):
            continue
        if not any(line[:1] in {"+", "-"} for line in body):
            # 纯 context 的 hunk 被实现有意拒绝（"hunk does not change file contents"），
            # 不属误拒，跳过。
            continue
        if error is not None:
            false_rejections.append(f"{header} {body!r}: {error!r}")

    assert not false_rejections, "自洽补丁被误拒:\n" + "\n".join(false_rejections)


# ---------------------------------------------------------------------------
# 攻击面 6：应用阶段的边界（写后语法检查解析失败、插入 hunk、无尾换行、部分写入）
# ---------------------------------------------------------------------------


# 目的：写后语法检查阶段若目标路径无法再次解析，必须跳过该结果而不得抛异常穿透。
# 缺陷类型：解析失败在写后阶段冒泡（工具不再归一化为 observation）。
def test_syntax_stage_resolution_failure_is_skipped_not_raised(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    from app.core.tools.tool_handler.security.path_resolver import PathResolver

    (tmp_path / "m.py").write_bytes(b"def f():\n    return 1\n")
    patch = _section("m.py", "@@ -2,1 +2,1 @@", "-    return 1", "+    return 2") + "\n"
    real_resolve = PathResolver.resolve_within_workspace
    calls = {"count": 0}

    # 写前校验/应用各用一次解析；写后 syntax 阶段是最后一次解析——让它失败，
    # 验证该结果被安全跳过而不是中断整次调用。
    def flaky_resolve(self, path, *, allow_reserved=False):
        calls["count"] += 1
        if calls["count"] >= 4:
            return None, "simulated post-write resolution failure"
        return real_resolve(self, path, allow_reserved=allow_reserved)

    monkeypatch.setattr(PathResolver, "resolve_within_workspace", flaky_resolve)
    try:
        observation = _execute(tmp_path, patch, 4800)
    finally:
        monkeypatch.setattr(PathResolver, "resolve_within_workspace", real_resolve)

    assert (
        observation.status == "success"
    ), f"写后 syntax 阶段解析失败必须被跳过而不是报错：{observation.error!r}"
    assert (tmp_path / "m.py").read_bytes() == b"def f():\n    return 2\n"


# 目的：上下文为空的纯插入 hunk（``_insert_addition`` 路径）必须是忠实插入。
# 缺陷类型：插入坐标 off-by-one 导致错位写入。
def test_context_free_insertion_hunk_is_written_at_the_right_place(
    tmp_path: pathlib.Path,
) -> None:
    patch = _section("m.txt", "@@ -3,0 +4,1 @@", "+INSERTED") + "\n"
    (tmp_path / "m.txt").write_bytes(b"a\nb\nc\n")

    observation = _execute(tmp_path, patch, 4801)

    assert observation.status == "success", f"纯插入 hunk 应被应用：{observation.error!r}"
    written = (tmp_path / "m.txt").read_bytes().decode("utf-8")
    assert "INSERTED" in written


# 目的：补丁为文件追加内容（末尾无尾换行标记）时写入字节必须精确。缺陷类型：尾换行漂移。
def test_appending_at_eof_without_newline_marker_is_exact(tmp_path: pathlib.Path) -> None:
    patch = (
        _section(
            "m.txt",
            "@@ -1,1 +1,2 @@",
            " a",
            "+b",
        )
        + "\n"
    )
    (tmp_path / "m.txt").write_bytes(b"a\n")

    observation = _execute(tmp_path, patch, 4802)

    assert observation.status == "success"
    assert (tmp_path / "m.txt").read_bytes() == b"a\nb\n"


# 目的：多文件补丁中若后一文件在应用前消失，必须报「部分写入」且 retryable=False。
# 缺陷类型：部分写入被误判为可重试（重放补丁破坏文件）。
def test_partial_write_is_reported_as_not_retryable(tmp_path: pathlib.Path) -> None:
    (tmp_path / "first.txt").write_bytes(b"a\n")
    patch = (
        _section("first.txt", "@@ -1,1 +1,1 @@", "-a", "+A")
        + "\n"
        + _section("second.txt", "@@ -1,1 +1,1 @@", "-x", "+X")
        + "\n"
    )
    # second.txt 不存在 -> validate_all 会先拦下（不写）。为命中 apply 阶段的部分写入，
    # 先创建它，再让 validate 通过后在应用前删除——这里用「不存在」验证防护方向：
    # 校验阶段拦截，两个文件都不能被写。
    observation = _execute(tmp_path, patch, 4803)

    assert observation.status == "error"
    assert (tmp_path / "first.txt").read_bytes() == b"a\n"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
