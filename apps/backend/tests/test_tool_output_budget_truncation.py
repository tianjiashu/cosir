"""``ToolOutputBudget`` 截断策略回归：头尾保留、标记口径、预算不变量与降级路径。

只测不改：不修改 ``apps/backend/app/`` 下任何生产代码。

覆盖维度：
  A. 未超限：原对象返回；``content=None`` 归一为空串。
  B. 头尾保留：超限输出的开头上下文与末尾结论/报错都可见（旧实现只留开头）。
  C. 标记口径：省略字符数、artifact 路径都出现在正文里，且标记本身不被预算切碎。
  D. 行边界：保留下来的正文行都是完整行，不出现半行。
  E. 不变量：任意预算下可见正文长度 ``<= max_chars``；极小预算降级不抛异常。
  F. 无 workspace：标记说明完整输出不可取回，且不落盘。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.tools.guard.tool_output_budget import ToolOutputBudget
from app.core.tools.schemas import ToolExecutionContext, ToolObservation


def _ctx(root: Path) -> ToolExecutionContext:
    """构造绑定到给定 workspace 根的执行上下文（与既有对抗性测试同一口径）。"""

    return ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=root, run_id=1)


def _obs(content: str | None) -> ToolObservation:
    """构造待过预算的成功观察。"""

    return ToolObservation(tool_name="t", status="success", content=content)


def test_content_within_budget_is_returned_unchanged() -> None:
    """未超限时返回原对象，不加标记、不做复制。"""

    budget = ToolOutputBudget(max_chars=100)
    obs = _obs("短内容")

    assert budget.apply(obs, None) is obs


def test_content_exactly_at_budget_is_unchanged() -> None:
    """长度恰好等于预算属「未超限」，不应触发截断。"""

    budget = ToolOutputBudget(max_chars=10)
    obs = _obs("0123456789")

    result = budget.apply(obs, None)

    assert result is obs
    assert result.artifact_data == {}


def test_none_content_is_normalized_to_empty_string() -> None:
    """``content=None`` 归一为空串后仍在预算内，直接返回。"""

    budget = ToolOutputBudget(max_chars=10)
    obs = ToolObservation(tool_name="t", status="success", content=None)

    result = budget.apply(obs, None)

    assert result.content == ""


def test_head_and_tail_both_survive_truncation(tmp_path: Path) -> None:
    """核心改进：超限输出同时保留开头上下文与末尾结论，模型不必只靠 artifact 猜结尾。"""

    root = tmp_path / "ws"
    root.mkdir()
    budget = ToolOutputBudget(max_chars=400)
    head_marker = "HEAD_CONTEXT: building\n"
    tail_marker = "TAIL_ERROR: 1 assertion failed\n"
    content = head_marker + ("filler line\n" * 500) + tail_marker

    result = budget.apply(_obs(content), _ctx(root))

    assert result.content is not None
    assert len(result.content) <= 400
    assert result.content.startswith(head_marker)
    assert result.content.endswith(tail_marker)
    assert "output truncated" in result.content


def test_marker_reports_omitted_volume_and_artifact_path(tmp_path: Path) -> None:
    """标记给出省略量、原文总量与完整输出路径，且标记本身不被预算切碎。"""

    root = tmp_path / "ws"
    root.mkdir()
    budget = ToolOutputBudget(max_chars=1000)
    content = "".join(f"line-{i:05d}\n" for i in range(2000))

    result = budget.apply(_obs(content), _ctx(root))

    artifact_path = (result.artifact_data or {}).get("artifact_path")
    assert isinstance(artifact_path, str) and artifact_path.startswith(".cosir/tool-artifacts/")
    assert result.content is not None
    assert f"full output: {artifact_path}" in result.content
    assert f"of {len(content)} characters" in result.content
    # 标记闭合符仍在，说明标记没有被 max_chars 切掉尾部（旧实现会切掉路径）。
    assert "] ..." in result.content
    assert (result.artifact_data or {}).get("output_truncated") is True
    assert (result.artifact_data or {}).get("original_chars") == len(content)
    # 落盘内容仍是完整原文。
    assert (root / artifact_path).read_text(encoding="utf-8") == content


def test_marker_is_never_sliced_when_budget_fits(tmp_path: Path) -> None:
    """预算足以容纳完整标记时，标记不得被截断（旧的 ``hint[:max_chars]`` 会切碎它）。"""

    root = tmp_path / "ws"
    root.mkdir()
    content = "".join(f"row-{i:04d}\n" for i in range(400))

    for max_chars in (300, 1000, 3000):
        result = ToolOutputBudget(max_chars=max_chars).apply(_obs(content), _ctx(root))

        artifact_path = (result.artifact_data or {}).get("artifact_path")
        assert isinstance(artifact_path, str) and artifact_path
        assert result.content is not None
        assert result.content.count("[output truncated") == 1
        assert f"full output: {artifact_path}" in result.content
        assert "] ..." in result.content


def test_retained_body_lines_are_never_cut_in_half(tmp_path: Path) -> None:
    """头尾切口都落在行边界上：保留的正文行都是完整行。"""

    root = tmp_path / "ws"
    root.mkdir()
    budget = ToolOutputBudget(max_chars=300)
    content = "".join(f"row-{i:04d}\n" for i in range(1000))

    result = budget.apply(_obs(content), _ctx(root))

    assert result.content is not None
    body_lines = [
        line for line in result.content.split("\n") if line and "output truncated" not in line
    ]
    assert body_lines, "截断后应至少保留部分正文行"
    assert all(re.fullmatch(r"row-\d{4}", line) for line in body_lines)
    assert body_lines[0] == "row-0000"
    assert body_lines[-1] == "row-0999"


@pytest.mark.parametrize("max_chars", [1, 2, 5, 50, 137, 200, 1000, 20000])
def test_visible_content_never_exceeds_budget(tmp_path: Path, max_chars: int) -> None:
    """任意预算下可见正文长度都不超过预算，极小预算也不得抛异常。"""

    root = tmp_path / "ws"
    root.mkdir()
    content = "".join(f"line-{i:04d}\n" for i in range(500))

    result = ToolOutputBudget(max_chars=max_chars).apply(_obs(content), _ctx(root))

    assert result.content is not None
    assert len(result.content) <= max_chars


def test_tiny_budget_still_writes_full_artifact(tmp_path: Path) -> None:
    """极小预算下截断降级，但完整原文仍然落盘。"""

    root = tmp_path / "ws"
    root.mkdir()
    content = "z" * 500

    result = ToolOutputBudget(max_chars=5).apply(_obs(content), _ctx(root))

    assert result.content is not None
    assert len(result.content) <= 5
    artifact_path = (result.artifact_data or {}).get("artifact_path")
    assert isinstance(artifact_path, str) and artifact_path
    assert (root / artifact_path).read_text(encoding="utf-8") == content


def test_without_workspace_marker_says_output_unavailable() -> None:
    """无 workspace 时不落盘，标记明确说明完整输出不可取回。"""

    budget = ToolOutputBudget(max_chars=200)
    content = "x" * 5000

    result = budget.apply(_obs(content), None)

    assert result.content is not None
    assert len(result.content) <= 200
    assert "full output unavailable" in result.content
    assert (result.artifact_data or {}).get("artifact_path") == ""
