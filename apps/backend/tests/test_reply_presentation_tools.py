"""回复呈现工具：``present_files`` 只呈现通过校验的文件，``offer_next_step`` 当场返回、不挂起。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.agents.define_agents import main_agent
from app.core.tool.handler.offer_next_step import OfferNextStepTool
from app.tools.tool_handler.present_files import PresentFilesTool


def _context(root: Path) -> SimpleNamespace:
    return SimpleNamespace(workspace_root=root, tool_call_id="call_1", session_id=1)


@pytest.mark.usefixtures("backend_env")
def test_present_files_shows_valid_files_and_names_the_rest(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs" / "report.md").write_text("# 报告", encoding="utf-8")
    outside = tmp_path / "outside.html"
    outside.write_text("<p>页面</p>", encoding="utf-8")

    observation = PresentFilesTool().execute(
        files=[
            {"path": "docs/report.md", "caption": "  结论在第 2 节  "},
            {"path": str(outside), "caption": None},
            {"path": "docs", "caption": None},
            {"path": "missing.md", "caption": None},
            # 同一文件再写一遍：只留一行。
            {"path": "./docs/report.md", "caption": None},
        ],
        execution_context=_context(workspace),
    )

    assert observation.status == "success"
    assert observation.display_data == {
        "kind": "deliverables",
        "files": [
            {
                "path": "docs/report.md",
                "absolute_path": str((workspace / "docs" / "report.md").resolve()),
                "caption": None,
            },
            {
                "path": str(outside.resolve()),
                "absolute_path": str(outside.resolve()),
                "caption": None,
            },
        ],
    }
    # 没通过的只告诉模型，卡片里不出现。
    assert "Presented 2 file(s)" in observation.content
    assert "- docs: no such file" in observation.content
    assert "- missing.md: no such file" in observation.content


@pytest.mark.usefixtures("backend_env")
def test_present_files_fails_when_nothing_can_be_shown(tmp_path: Path) -> None:
    observation = PresentFilesTool().execute(
        files=[{"path": "missing.md", "caption": None}, {"path": "/dev/null", "caption": None}],
        execution_context=_context(tmp_path),
    )

    assert observation.status == "error"
    assert not (observation.display_data or {}).get("kind")
    assert "missing.md" in observation.content


def test_offer_next_step_returns_at_once_without_waiting() -> None:
    observation = OfferNextStepTool().execute(
        ask=" 要我把这个修复也应用到移动端吗？ ",
        replies=["好，应用", " 先不用 ", "好，应用"],
        execution_context=_context(Path(".")),
    )

    assert observation.status == "success"
    # 不是 HITL：不挂起、不登记等待，建议只存在于展示数据里。
    assert observation.suspend is False
    assert observation.display_data == {
        "kind": "next-step",
        "ask": "要我把这个修复也应用到移动端吗？",
        "replies": ["好，应用", "先不用"],
    }


@pytest.mark.parametrize(
    ("ask", "replies"),
    [("   ", ["好"]), ("继续吗？", ["  "]), ("继续吗？", ["x" * 25]), ("问" * 101, ["好"])],
)
def test_offer_next_step_rejects_labels_it_cannot_send_back(ask: str, replies: list[str]) -> None:
    observation = OfferNextStepTool().execute(
        ask=ask, replies=replies, execution_context=_context(Path("."))
    )

    assert observation.status == "error"
    assert not (observation.display_data or {}).get("kind")


def test_definitions_are_quiet_sidebar_only_and_given_to_the_main_agent() -> None:
    for tool in (PresentFilesTool(), OfferNextStepTool()):
        definition = tool.to_definition()
        assert definition.conversation_presentation == "quiet"
        assert definition.requires_user_surface
        assert definition.user_decision is None
        assert definition.name in main_agent().allowed_tools
