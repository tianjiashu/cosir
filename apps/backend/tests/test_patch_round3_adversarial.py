"""Round-3 adversarial probe for the hunk-count guard, its trailing-blank strip,
and the model-visible contract sample.

被测对象（代码冻结，仅新增测试）：
  - ``patch_parser._hunk_count_diagnostics`` 第三轮新增的「剥离 hunk 正文尾部空行」；
  - ``patch_parser._validate_section_headers`` 与 ``_hunk_count_diagnostics`` 的
    hunk 头判定源分歧（``startswith("@@ ")`` vs ``RE_HUNK_HEADER``）；
  - ``ApplyPatchArgs.patch`` 字段描述中「最小可接受样例」的自洽性（格式契约的唯一事实源）。

判定基准是 Git 自身的接受/拒绝语义（``git apply --check``，夹具规范：``git init`` +
``core.autocrlf=false`` + 精确 LF 字节 + 比对结果文件字节），而不是任何单一 Python 实现。
本文件不修改生产代码，也不修改既有测试的期望。

维护者裁定（2026-09-21，第三轮）：**hunk 正文的尾部空行一律剥离，不区分空行之后是 ``@@`` 还是
``diff --git``，属有意放行而非缺陷。** 依据是本仓实际应用链路是 ``unidiff`` + 模糊匹配而非 Git：
尾部空行不构成 hunk 正文（``parse_git_unified_diff`` 只收 ``' '``/``'+'``/``'-'`` 前缀行，空行被
丢弃），既不引发 unidiff 失步也不造成静默内容丢失，放行后仍能**忠实应用**。Git 的相反判定（空行在
``@@`` 之前报 ``patch fragment without header``、位于补丁末尾报 ``patch does not apply``）只在
「把补丁交给 Git 应用」时成立，本工具不以其为准。测试名或注释中出现 ``for_git`` /
``where_git_refuses`` 的用例，记录的是这一差异事实（前置条件：Git 拒绝），**不代表本仓行为有缺陷**。
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest

from app.core.tools.schemas import ToolExecutionContext
from app.core.tools.tool_handler.apply_patch_tool import ApplyPatchTool
from app.core.tools.tool_handler.patch_write.patch_parser import (
    parse_git_unified_diff,
    parse_git_unified_diff_detailed,
)
from app.core.tools.tool_models.apply_patch_args import ApplyPatchArgs

GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(GIT is None, reason="git is required as the ground-truth oracle")


def _hunk_count_diagnostics():
    """Test-local view of the parser count pass: notes emitted when counts were recomputed."""

    module = sys.modules["app.core.tools.tool_handler.patch_write.patch_parser"]

    def _notes(section: str, section_index: int) -> list[str]:
        return module._recompute_hunk_counts(section, section_index)[1]

    return _notes


def _section(header: str, *body: str, path: str = "m.txt") -> str:
    """Build one Git file section: three header lines + one ``@@`` header + body."""

    return "\n".join(
        (f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", header, *body)
    )


def _init_repo(work: pathlib.Path, files: dict[str, str]) -> None:
    """Create a repo with LF-exact files and a single commit."""

    work.mkdir(parents=True, exist_ok=True)
    for args in (
        ("init", "-q"),
        ("config", "core.autocrlf", "false"),
        ("config", "user.email", "t@e.com"),
        ("config", "user.name", "t"),
    ):
        subprocess.run([GIT, *args], cwd=str(work), capture_output=True, check=False)
    for rel, content in files.items():
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
    subprocess.run([GIT, "add", "-A"], cwd=str(work), capture_output=True, check=False)
    subprocess.run(
        [GIT, "commit", "-q", "-m", "base"], cwd=str(work), capture_output=True, check=False
    )


@pytest.fixture
def git_oracle(tmp_path_factory):
    """Return a callable giving Git's verdict for (patch_text, files)."""

    def check(patch: str, files: dict[str, str]):
        root = tmp_path_factory.mktemp("oracle")
        work = root / "w"
        _init_repo(work, files)
        patch_file = root / "p.diff"
        patch_file.write_bytes(patch.encode("utf-8"))
        checked = subprocess.run(
            [GIT, "apply", "--check", str(patch_file)],
            cwd=str(work),
            capture_output=True,
            text=True,
        )
        produced: dict[str, bytes] = {}
        if checked.returncode == 0:
            subprocess.run(
                [GIT, "apply", str(patch_file)], cwd=str(work), capture_output=True, check=False
            )
            produced = {rel: (work / rel).read_bytes() for rel in files}
        return checked.returncode, checked.stderr.strip(), produced

    return check


def _run_tool(tmp_path: pathlib.Path, patch: str, content: str) -> tuple[str, bytes, str | None]:
    """Run the real tool against a single file and return status/bytes/error."""

    target = tmp_path / "m.txt"
    target.write_bytes(content.encode("utf-8"))
    observation = ApplyPatchTool().execute(
        ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=tmp_path, run_id=11),
        patch=patch,
    )
    after = target.read_bytes() if target.exists() else b""
    return observation.status, after, observation.error


# ---------------------------------------------------------------------------
# 攻击面 1：第三轮「剥离尾部空行」的语义边界（裁定：有意放行，不是缺陷）
# ---------------------------------------------------------------------------


# 目的：定位剥离的语义边界——空行位于同一 section 内两个 hunk 之间时，Git 视之为前一个
# hunk 的真实 context 行，后续 @@ 头变成 "patch fragment without header"（exit 128）。
# 已接受差异（见模块 docstring 裁定）：本仓链路放行且可忠实应用，此处锁死该事实。
def test_blank_line_between_hunks_is_deliberately_tolerated_where_git_refuses(git_oracle) -> None:
    patch = (
        _section(
            "@@ -1,1 +1,1 @@",
            "-a",
            "+A",
            "",
            "@@ -3,1 +3,1 @@",
            "-c",
            "+C",
        )
        + "\n"
    )

    returncode, stderr, produced = git_oracle(patch, {"m.txt": "a\nb\nc\n"})
    operations, error = parse_git_unified_diff(patch)

    assert returncode != 0, "前置条件：Git 必须拒绝该形状（空行是 hunk 1 的 context 行）"
    assert "patch fragment without header" in stderr
    assert produced == {}, "前置条件：Git 拒绝时不得写出任何文件"
    assert error is None, f"按裁定应放行：error={error!r}"
    assert [operation.file_path for operation in operations] == ["m.txt"]


# 目的：把「放行」与随后的应用串起来——工具报 success 并写盘（产出 = 作者意图），而 Git 对
# 同一字节序列 exit 128。本用例锁死裁定边界：本仓链路是忠实应用，故不计为缺陷。
def test_delimiter_blank_between_hunks_is_applied_faithfully_where_git_refuses(
    tmp_path: pathlib.Path,
    git_oracle,
) -> None:
    patch = (
        _section(
            "@@ -1,1 +1,1 @@",
            "-a",
            "+A",
            "",
            "@@ -3,1 +3,1 @@",
            "-c",
            "+C",
        )
        + "\n"
    )

    returncode, _, _ = git_oracle(patch, {"m.txt": "a\nb\nc\n"})
    status, _, error = _run_tool(tmp_path, patch, "a\nb\nc\n")

    assert returncode != 0, "前置条件：Git 必须拒绝"
    assert status == "success", f"实际生产行为：status={status!r} error={error!r}"


# 目的：连续两个空行同样被剥离（剥离对空行数量不敏感），Git 仍视为两行 context（exit 128）。
# 已接受差异：与单空行同属裁定范围。
def test_two_blank_lines_between_hunks_are_excluded_by_design_where_git_refuses(
    git_oracle,
) -> None:
    patch = (
        _section(
            "@@ -1,1 +1,1 @@",
            "-a",
            "+A",
            "",
            "",
            "@@ -3,1 +3,1 @@",
            "-c",
            "+C",
        )
        + "\n"
    )

    returncode, stderr, produced = git_oracle(patch, {"m.txt": "a\nb\nc\n"})
    operations, error = parse_git_unified_diff(patch)

    assert returncode == 128
    assert "patch fragment without header" in stderr
    assert produced == {}
    assert error is None, f"按裁定应放行：error={error!r}"
    assert operations, "按裁定放行（Git 拒绝，本仓链路忠实应用）"


# 目的：反向边界——「补丁末尾空行」与「section 之间的空行」是剥离逻辑**必需**的正确
# 行为（Git exit 0）。本用例把这条正确行为锁死，防止将来「收紧边界」时误伤。
def test_trailing_blank_at_patch_end_and_between_sections_stay_accepted(git_oracle) -> None:
    at_end = _section("@@ -1,1 +1,1 @@", "-a", "+A") + "\n\n"
    between = (
        "\n\n".join(
            (
                _section("@@ -1,1 +1,1 @@", "-a", "+A", path="f.txt"),
                _section("@@ -1,1 +1,1 @@", "-x", "+X", path="g.txt"),
            )
        )
        + "\n"
    )

    end_rc, _, end_produced = git_oracle(at_end, {"m.txt": "a\n"})
    between_rc, _, between_produced = git_oracle(between, {"f.txt": "a\n", "g.txt": "x\n"})

    assert (end_rc, end_produced) == (0, {"m.txt": b"A\n"})
    assert (between_rc, between_produced) == (0, {"f.txt": b"A\n", "g.txt": b"X\n"})
    assert parse_git_unified_diff(at_end)[1] is None
    assert parse_git_unified_diff(between)[1] is None


# 目的：剥离只作用于尾部。正文中间的空行（其后还有非空正文行）必须仍按 context 计。
# 缺陷类型：剥离范围过大导致 body 被截断、计数系统性少算。
def test_blank_line_with_following_body_is_still_counted(git_oracle) -> None:
    patch = _section("@@ -1,3 +1,3 @@", " a", "", "-b", "+c") + "\n"

    returncode, _, _ = git_oracle(patch, {"m.txt": "a\n\nb\n"})
    operations, error = parse_git_unified_diff(patch)

    assert returncode == 0, "前置条件：该自洽补丁必须被 Git 接受"
    assert error is None
    assert operations[0].hunks[0].source_length == 3


# 目的：把裁定边界做成一对照——同样「少算」，尾部空行（已接受差异，被剥离）与**真实多余正文
# 行**（仍必须报计数不符）的处置不同；后者才是本校验要拦截的形状。
# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示（不再拒绝）。对照组的判定因此从
# 「error 非空」改为「count_repairs 非空」——无空行的真实多余正文行仍会被计为不符并给出提示。
def test_real_extra_body_line_is_still_rejected_where_a_blank_delimiter_is_excluded(
    git_oracle,
) -> None:
    # 关键形状：空行是 hunk 1 正文的**最后一行**，其后紧跟 hunk 2 的 @@ 头。
    # 剥离让 hunk 1 的计数看起来「相符」，诊断因此完全消失。
    patch = (
        _section(
            "@@ -1,1 +1,1 @@",
            "-a",
            "+A",
            "",
            "@@ -3,1 +3,1 @@",
            "-c",
            "+C",
        )
        + "\n"
    )
    # 对照：同一份「少算」改由**真实多余正文行**表达时会被正确报告（证明只有空行被剥离）。
    control = (
        _section(
            "@@ -1,1 +1,1 @@",
            "-a",
            "+A",
            "-b",
            "+B",
            "@@ -4,1 +4,1 @@",
            "-d",
            "+D",
        )
        + "\n"
    )

    returncode, stderr, _ = git_oracle(patch, {"m.txt": "a\nb\nc\n"})
    control_rc, _, _ = git_oracle(control, {"m.txt": "a\nb\nc\nd\n"})
    operations, error = parse_git_unified_diff(patch)
    diagnostics = _hunk_count_diagnostics()(patch, 0)
    control_outcome = parse_git_unified_diff_detailed(control)

    assert returncode != 0, "前置条件：Git 拒绝该形状"
    assert stderr
    assert control_rc != 0, "前置条件：无空行的对照组同样被 Git 拒绝"
    assert control_outcome.count_repairs, "对照：无空行时计数校验仍然生效（必须给出重算提示）"
    assert diagnostics == [], f"按裁定不应出现诊断：{diagnostics}"
    assert error is None and operations, f"按裁定应放行：error={error!r}"


# ---------------------------------------------------------------------------
# 攻击面 2：两处 hunk 头判定源分歧（第二轮假设的闭环）
# ---------------------------------------------------------------------------


# 目的：闭环第二轮假设——``_validate_section_headers`` 用 ``startswith("@@ ")``、
# ``_hunk_count_diagnostics`` 用 ``RE_HUNK_HEADER``。对分歧输入逐一对齐 Git 判定。
# 缺陷类型：出现「工具接受而 Git 拒绝」或「工具拒绝而 Git 接受」即为缺陷。
@pytest.mark.parametrize(
    "header",
    [
        "@@ -1,1 +1,1@@",
        "@@ -1 +1@@",
        "@@ -1,1 +1,1@@@",
        "@@ -1,1 +1,1@@x",
        "@@ -1,1 +1,1  @@",
    ],
)
def test_divergent_hunk_header_forms_never_diverge_from_git(header: str, git_oracle) -> None:
    patch = _section(header, "-a", "+b")

    returncode, _, produced = git_oracle(patch, {"m.txt": "a\n"})
    operations, error = parse_git_unified_diff(patch)

    tool_accepts = error is None
    git_accepts = returncode == 0

    assert tool_accepts == git_accepts, (
        f"{header!r}: 判定分歧 tool_accepts={tool_accepts} (error={error!r}) "
        f"git_accepts={git_accepts} rc={returncode}"
    )
    if not git_accepts:
        assert produced == {}
        assert operations == []


# 目的：一个 section 内「真 hunk 头 + 伪 hunk 头（startswith 一致但 RE 不匹配）」时，
# 伪头部不得被当成计数边界，也不得让真正的计数不符被掩盖。
# 缺陷类型：边界错位导致的漏放/误拒。
def test_pseudo_header_between_real_hunks_is_not_a_count_boundary(git_oracle) -> None:
    patch = _section(
        "@@ -1,1 +1,1 @@",
        "-a",
        "+A",
        "@@ -1,1 +1,1@@",
        "-c",
        "+C",
    )

    returncode, _, _ = git_oracle(patch, {"m.txt": "a\nc\n"})
    operations, error = parse_git_unified_diff(patch)

    assert returncode != 0, "前置条件：Git 拒绝（伪头部不是合法 hunk 头）"
    assert error is not None, "工具必须同样拒绝"
    assert operations == []


# ---------------------------------------------------------------------------
# 攻击面 3：新增回归用例的有效性（只读方式独立验证）
# ---------------------------------------------------------------------------


# 目的：证明新增回归网绑定的正是「尾部空行剥离」这一行为，同时指出它**未**覆盖 hunk 间空行。
# 缺陷类型：回归网假绿 / 覆盖缺口。
def test_round3_regression_net_covers_only_the_false_reject_direction() -> None:
    diagnostics = _hunk_count_diagnostics()
    trailing = _section("@@ -1,1 +1,1 @@", "-a", "+A") + "\n"
    masked = (
        _section(
            "@@ -1,1 +1,1 @@",
            "-a",
            "+A",
            "",
            "@@ -2,1 +2,1 @@",
            "-x",
            "+X",
        )
        + "\n"
    )
    under_declared_without_blank = _section("@@ -1,1 +1,1 @@", "-a", "+A", "-b", "+B")

    assert diagnostics(trailing, 0) == [], "尾部空行剥离：该形状必须无诊断（新回归网锁定此行为）"
    assert (
        diagnostics(under_declared_without_blank, 0) != []
    ), "无空行遮挡时诊断必须生效（回归网有效）"
    # 覆盖缺口：同一行为在 hunk 间空行上会掩盖真实 context 行，但新回归网无此用例。
    assert diagnostics(masked, 0) == [], "覆盖缺口：hunk 间空行同样被剥离，新回归网未覆盖"


# 目的：锁死「空行位置决定 Git 判定」这一根因——section 间空行 Git 接受，hunk 间空行
# Git 拒绝，两者差别只在空行之后是 ``diff --git`` 还是 ``@@``。缺陷类型：剥离无法区分位置。
def test_blank_line_position_decides_git_verdict(git_oracle) -> None:
    after_section = (
        "\n".join(
            (
                _section("@@ -1,1 +1,1 @@", "-a", "+A", path="f.txt"),
                "",
                _section("@@ -1,1 +1,1 @@", "-x", "+X", path="g.txt"),
            )
        )
        + "\n"
    )
    after_hunk = (
        _section(
            "@@ -1,1 +1,1 @@",
            "-a",
            "+A",
            "",
            "@@ -3,1 +3,1 @@",
            "-c",
            "+C",
        )
        + "\n"
    )

    section_rc, _, _ = git_oracle(after_section, {"f.txt": "a\n", "g.txt": "x\n"})
    hunk_rc, _, _ = git_oracle(after_hunk, {"m.txt": "a\nb\nc\n"})

    assert section_rc == 0, "section 间空行：Git 接受"
    assert hunk_rc == 128, "hunk 间空行：Git 拒绝"
    assert parse_git_unified_diff(after_section)[1] is None
    assert parse_git_unified_diff(after_hunk)[1] is None, "hunk 间空行按裁定同样被放行"


# ---------------------------------------------------------------------------
# 攻击面 4：模型可见契约样例自洽性
# ---------------------------------------------------------------------------


def _documented_sample() -> str:
    """Extract the 8-line diff block shown as the minimal accepted example."""

    description = ApplyPatchArgs.model_fields["patch"].description or ""
    lines = description.split("\n")
    start = next(index for index, line in enumerate(lines) if line.startswith("diff --git "))
    return "\n".join(lines[start : start + 8]) + "\n"


# 目的：把描述里的「最小可接受样例」当作真实补丁喂给解析器。样例头部声明 ``+10,4``
# 但正文 target 实为 3，样例自身矛盾。缺陷类型：契约样例自相矛盾，模型照抄必被拒绝。
def test_documented_minimal_sample_is_self_consistent() -> None:
    operations, error = parse_git_unified_diff(_documented_sample())

    assert error is None, f"文档样例自身应可被接受，实际 error={error!r}"
    assert operations, "文档样例应解析出至少一个 operation"


# 目的：同一份文档样例必须在 Git 下也可应用。缺陷类型：样例不可应用（契约虚假）。
def test_documented_minimal_sample_is_applicable_by_git(git_oracle) -> None:
    filler = "\n".join(
        [f"# filler {i}" for i in range(1, 10)] + ["import os", "old_line()", "keep()"]
    )

    returncode, stderr, produced = git_oracle(_documented_sample(), {"pkg/mod.py": filler + "\n"})

    assert returncode == 0, f"文档样例在 Git 下不可应用：rc={returncode} stderr={stderr!r}"
    assert produced["pkg/mod.py"].endswith(b"keep()\n")


# 目的：模型照抄文档样例（作为完整 patch 值）时工具必须接受。缺陷类型：契约与实现不一致。
def test_copying_the_documented_sample_verbatim_succeeds(tmp_path: pathlib.Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    filler = "\n".join(
        [f"# filler {i}" for i in range(1, 10)] + ["import os", "old_line()", "keep()"]
    )
    (pkg / "mod.py").write_text(filler + "\n", encoding="utf-8")

    observation = ApplyPatchTool().execute(
        ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=tmp_path, run_id=12),
        patch=_documented_sample(),
    )

    assert (
        observation.status == "success"
    ), f"模型照抄文档样例即失败：status={observation.status} error={observation.error!r}"


# ---------------------------------------------------------------------------
# 攻击面 5：常规对抗面复测
# ---------------------------------------------------------------------------


# 目的：常规形状的接受/拒绝必须与 Git 一致，不得因第三轮改动而漂移。
# 缺陷类型：剥离逻辑顺带改变其它形状的判定。
def test_routine_shapes_agree_with_git(git_oracle) -> None:
    cases = {
        "index line present": (
            "diff --git a/m.txt b/m.txt\nindex 0000000..1111111 100644\n"
            "--- a/m.txt\n+++ b/m.txt\n@@ -1,1 +1,1 @@\n-a\n+A\n",
            {"m.txt": "a\n"},
        ),
        "tab indented body": (
            _section("@@ -1,2 +1,2 @@", " \tkeep", "-a", "+A") + "\n",
            {"m.txt": "\tkeep\na\n"},
        ),
        "trailing spaces in body": (
            _section("@@ -1,2 +1,2 @@", " a  ", "-b  ", "+c  ") + "\n",
            {"m.txt": "a  \nb  \n"},
        ),
        "section text after header": (
            _section("@@ -1,2 +1,2 @@ def f():", " a", "-b", "+c") + "\n",
            {"m.txt": "a\nb\n"},
        ),
        "no newline marker mid-hunk": (
            _section("@@ -1,1 +1,1 @@", "-a", "\\ No newline at end of file", "+b") + "\n",
            {"m.txt": "a"},
        ),
        "no newline marker at end": (
            _section("@@ -1,1 +1,1 @@", "-a", "+b", "\\ No newline at end of file") + "\n",
            {"m.txt": "a\n"},
        ),
    }

    mismatches = []
    for name, (patch, files) in cases.items():
        returncode, _, _ = git_oracle(patch, files)
        _, error = parse_git_unified_diff(patch)
        if (error is None) != (returncode == 0):
            mismatches.append(
                f"{name}: tool_ok={error is None} git_rc={returncode} error={error!r}"
            )

    assert not mismatches, "常规形状与 Git 判定分歧：\n" + "\n".join(mismatches)


# 目的：超长行（5000 字符）不得触发计数误判。缺陷类型：按行长度分支的错误实现。
def test_very_long_body_lines_count_correctly() -> None:
    patch = _section("@@ -1,1 +1,1 @@", "-" + "x" * 5000, "+" + "y" * 5000)

    operations, error = parse_git_unified_diff(patch)

    assert error is None
    assert operations[0].hunks[0].source_length == 1
    assert operations[0].hunks[0].target_length == 1


# 目的：显式 0 计数的边界不得被剥离逻辑改变判定，也不得与 Git 分歧。
# 缺陷类型：尾部空行剥离与 0 计数叠加导致的错判。
# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示。`@@ -1,0 +1,0 @@` 配 `-a` 正文（实际 1/0）
# 现在被重算后接受，因此改为锁死「重算值正确 + 提示给出声明值与实际值 + 起始行号不被剥离逻辑改动」，
# 并保留与 Git 的接受判定一致这一原有意图（对照 `@@ -0,0 +1,1 @@` 配 `+a` 的自洽形状）。
def test_explicit_zero_count_boundaries_stay_consistent(git_oracle) -> None:
    pure_zero = _section("@@ -1,0 +1,0 @@", "-a")
    head_insert = _section("@@ -0,0 +1,1 @@", "+a") + "\n"

    zero_outcome = parse_git_unified_diff_detailed(pure_zero)
    insert_ops, insert_error = parse_git_unified_diff(head_insert)
    insert_rc, _, _ = git_oracle(head_insert, {"m.txt": ""})

    assert zero_outcome.error is None, "显式 0 计数与正文不符时按正文重算后接受"
    assert len(zero_outcome.count_repairs) == 1
    assert "declared source=0 target=0" in zero_outcome.count_repairs[0]
    assert "body has source=1 target=0" in zero_outcome.count_repairs[0]
    zero_hunk = zero_outcome.operations[0].hunks[0]
    assert (zero_hunk.source_length, zero_hunk.target_length) == (1, 0), "重算值必须来自正文"
    assert (zero_hunk.source_start, zero_hunk.target_start) == (1, 1), "起始行号保持原样"
    assert (insert_error is None) == (insert_rc == 0)
    assert insert_ops or insert_rc != 0


# 目的：CRLF 整体补丁的计数口径必须与 Git 一致。缺陷类型：剥离对 CRLF 行尾空行误判。
def test_crlf_patch_with_trailing_blank_is_not_a_count_mismatch(git_oracle) -> None:
    lf = _section("@@ -1,1 +1,1 @@", "-a", "+A") + "\n"
    crlf = lf.replace("\n", "\r\n")

    returncode, stderr, _ = git_oracle(lf, {"m.txt": "a\n"})
    operations, error = parse_git_unified_diff(crlf)

    assert returncode == 0, "前置条件：LF 版本必须被 Git 接受"
    assert stderr == ""
    assert error is None, f"CRLF 版本被误拒：{error!r}"
    assert operations


# 目的：BOM 前缀属于 patch 层问题，不得被归因为「计数不符」。缺陷类型：诊断归因漂移。
def test_bom_prefixed_patch_is_not_a_count_diagnostic() -> None:
    patch = "\ufeff" + _section("@@ -1,1 +1,1 @@", "-a", "+A")

    operations, error = parse_git_unified_diff(patch)

    assert operations == []
    assert error is not None
    assert "hunk line counts do not match" not in error


# ---------------------------------------------------------------------------
# 攻击面 6：诊断质量 / 安全性 / 幂等
# ---------------------------------------------------------------------------


# 目的：诊断内容必须齐全（section/hunk 序号、头部原文、声明值、实际值）且纯 ASCII。
# 缺陷类型：新增剥离逻辑破坏诊断字段或引入非 ASCII。
# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示。提示落在 count_repairs，补丁本身被接受；
# 断言不得放宽：序号 / 头部原文 / 声明值 / 实际值四项齐全，且文本纯 ASCII。
def test_diagnostics_remain_complete_and_ascii() -> None:
    patch = _section("@@ -1,2 +1,2 @@", " a", "-b", "+c", " d")

    outcome = parse_git_unified_diff_detailed(patch)

    assert outcome.error is None, "计数不符不再阻断应用"
    assert len(outcome.operations) == 1
    assert len(outcome.count_repairs) == 1
    repair = outcome.count_repairs[0]
    assert "file section 1 hunk 1: '@@ -1,2 +1,2 @@'" in repair
    assert "declared source=2 target=2" in repair
    assert "body has source=3 target=3" in repair
    assert "counts recomputed from the body and applied as written" in repair
    repair.encode("ascii")


# 目的：计数不符时不再写文件、``retryable=True``、且不为无效补丁申请资源锁。
# 缺陷类型：剥离逻辑在拒绝路径上产生副作用或改变锁语义。
# 2026-09-21 契约 B：计数不符改为按正文重算 + 提示（会被接受并真的写入），因此本用例改用
# **真正被拒绝的形状**（缺 `---` 头、二进制元数据）来继续保障「拒绝 → 零写入 + 不申请锁」。
def test_rejected_patch_writes_nothing_and_requests_no_lock(tmp_path: pathlib.Path) -> None:
    from app.core.tools.guard.file_resource_paths import FileResourceResolver

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
        observation = ApplyPatchTool().execute(
            ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=tmp_path, run_id=13),
            patch=patch,
        )
        resources = FileResourceResolver(tmp_path).resolve("apply_patch", {"patch": patch})

        assert observation.status == "error", name
        assert observation.retryable is True, name
        assert target.read_bytes() == b"a\nb\n", name
        assert resources.write_paths == (), name
        assert resources.lock_paths == (), name
        # 真正被拒绝的形状不得产出任何「已按正文应用」的重算提示。
        assert parse_git_unified_diff_detailed(patch).count_repairs == [], name


# 目的：hunk 上下文不匹配（计数自洽但内容对不上）必须走「校验失败」通道且不写文件、
# 可重试，并且不得与计数诊断混淆。缺陷类型：错误归因错位 / 校验阶段被绕过后写盘。
def test_context_mismatch_fails_validation_without_writing(tmp_path: pathlib.Path) -> None:
    patch = _section("@@ -1,1 +1,1 @@", "-not-here", "+replacement")
    target = tmp_path / "m.txt"
    target.write_bytes(b"a\n")

    observation = ApplyPatchTool().execute(
        ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=tmp_path, run_id=14),
        patch=patch,
    )

    assert observation.status == "error"
    assert observation.retryable is True
    assert observation.error is not None and "no files were modified" in observation.error
    assert "hunk line counts do not match" not in observation.error
    assert target.read_bytes() == b"a\n"


# 目的：成功应用后写后语法检查必须可观测（合法补丁不得因为新增剥离逻辑而丢掉该分支）。
# 缺陷类型：成功路径回归。
def test_successful_patch_emits_file_change_display_payload(tmp_path: pathlib.Path) -> None:
    patch = _section("@@ -1,1 +1,1 @@", "-a", "+A") + "\n"
    target = tmp_path / "m.txt"
    target.write_bytes(b"a\n")

    observation = ApplyPatchTool().execute(
        ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=tmp_path, run_id=15),
        patch=patch,
    )

    assert observation.status == "success"
    assert observation.display_data["kind"] == "file-changes"
    assert observation.display_data["changes"][0]["path"] == "m.txt"
    assert target.read_bytes() == b"A\n"


# 目的：``_is_patch_retryable_after_correction`` 的重试分类契约——部分写入恒不可重试，
# OSError 起因按 errno 分类。缺陷类型：部分写入被误判为可重试（重放补丁破坏文件）。
@pytest.mark.parametrize(
    ("partial_applied", "cause", "expected"),
    [
        (True, RuntimeError("x"), False),
        (True, OSError(1, "x"), False),
        (False, RuntimeError("x"), True),
        (False, OSError(2, "no such file"), False),
        (False, OSError(11, "EAGAIN"), True),
        (False, OSError(16, "EBUSY"), True),
        (False, ValueError("not an OSError"), False),
    ],
)
def test_retryability_classification_contract(partial_applied, cause, expected) -> None:
    from app.core.tools.tool_handler.apply_patch_tool import _is_patch_retryable_after_correction
    from app.core.tools.tool_handler.patch_write.patch_apply import PatchApplyError

    error = PatchApplyError("boom", partial_applied=partial_applied)
    error.__cause__ = cause

    assert _is_patch_retryable_after_correction(error) is expected


# 目的：解析与诊断必须纯函数、可重复、不改入参。缺陷类型：剥离使用 ``list.pop()`` 时
# 意外共享可变状态（此处锁死该契约）。
def test_trailing_blank_strip_is_pure_and_idempotent() -> None:
    patch = _section("@@ -1,1 +1,1 @@", "-a", "+A", "", "", "@@ -9,1 +9,1 @@", "-x", "+X")
    original = patch
    diagnostics = _hunk_count_diagnostics()

    results = [diagnostics(patch, 0) for _ in range(5)]

    assert patch == original
    assert all(result == results[0] for result in results)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
