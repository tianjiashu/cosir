"""回复呈现工具：``present_deliverables`` 交付时观察每一项并记成交付记录，``offer_next_step`` 当场返回、不挂起。"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.assistant_transport.projection.tool_result import project_tool_result
from app.core.agents.define_agents import main_agent
from app.core.tool.handler.offer_next_step import OfferNextStepTool
from app.models.deliverable import DeliveredDirectory, DeliveredFile, DeliveredUrl
from app.models.message_metadata import ToolMessageMetadata
from app.tools.tool_handler import present_deliverables as present_module
from app.tools.tool_handler.present_deliverables import PresentDeliverablesTool
from app.utils.path.file_identity import UnsupportedFileSystemError, canonical_path


def _context(root: Path) -> SimpleNamespace:
    return SimpleNamespace(workspace_root=root, tool_call_id="call_1", session_id=1)


def _present(root: Path, *items: tuple[str, str | None], note: str | None = None):
    return PresentDeliverablesTool().execute(
        items=[{"target": target, "caption": caption} for target, caption in items],
        note=note,
        execution_context=_context(root),
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "report.md").write_text("# 报告", encoding="utf-8")
    (root / "dist").mkdir()
    return root


@pytest.mark.usefixtures("backend_env")
def test_observes_files_directories_and_web_addresses(workspace: Path, tmp_path: Path) -> None:
    """三种条目各记交付那一刻的观察；工作区外的文件以真实路径所在目录为根观察，链接记录它的目标。"""
    outside = tmp_path / "outside" / "page.html"
    outside.parent.mkdir()
    outside.write_text("<p>页面</p>", encoding="utf-8")
    (tmp_path / "alias.html").symlink_to(outside)

    observation = _present(
        workspace,
        ("docs/report.md", "  结论在第 2 节  "),
        ("dist", None),
        (str(tmp_path / "alias.html"), None),
        ("http://localhost:5173", "本地预览"),
    )

    assert observation.status == "success"
    assert observation.display_data == {"kind": "deliverables", "note": None}
    assert observation.deliverables == (
        DeliveredFile(
            workspace_path="docs/report.md",
            absolute_path=str(canonical_path(workspace / "docs" / "report.md")),
            caption="结论在第 2 节",
            kind="markdown",
            size=len("# 报告".encode()),
            width=None,
            height=None,
        ),
        DeliveredDirectory(
            workspace_path="dist",
            absolute_path=str(canonical_path(workspace / "dist")),
            caption=None,
        ),
        DeliveredFile(
            workspace_path=None,
            absolute_path=str(canonical_path(outside)),
            caption=None,
            kind="text",
            size=outside.stat().st_size,
            width=None,
            height=None,
        ),
        DeliveredUrl(url="http://localhost:5173", caption="本地预览"),
    )
    assert "Presented 4 item(s)" in observation.content


@pytest.mark.usefixtures("backend_env")
def test_failures_are_listed_per_item_and_do_not_block_the_rest(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不存在、设备路径、不是网址的网址、观察时无权访问或磁盘不提供文件号：各自进失败名单，其余照常。"""
    (workspace / "denied.txt").write_text("x")
    (workspace / "nfs.txt").write_text("x")
    real = present_module.canonical_path

    def flaky(path):
        if Path(path).name == "denied.txt":
            raise PermissionError("operation not permitted")
        if Path(path).name == "nfs.txt":
            raise UnsupportedFileSystemError(95, "no file ids")
        return real(path)

    monkeypatch.setattr(present_module, "canonical_path", flaky)

    observation = _present(
        workspace,
        ("docs/report.md", None),
        ("missing.md", None),
        ("/dev/null", None),
        ("https://", None),
        ("denied.txt", None),
        ("nfs.txt", None),
    )

    assert observation.status == "success"
    assert [item.workspace_path for item in observation.deliverables] == ["docs/report.md"]
    for target in ("missing.md", "/dev/null", "https://", "denied.txt", "nfs.txt"):
        assert f"- {target}:" in observation.content


_URL_SAMPLES = json.loads(
    (Path(__file__).parent / "fixtures" / "deliverable_urls.json").read_text("utf-8")
)


@pytest.mark.parametrize("url", _URL_SAMPLES["accepted"])
def test_deliverable_address_accepts_what_browsers_parse(url: str) -> None:
    assert DeliveredUrl(url=url, caption=None).url == url


# 潜在缺陷：后端收下浏览器解析不了的地址，前端渲染整组卡片时抛错；或判等 key 在使用时才抛错。
@pytest.mark.parametrize("url", _URL_SAMPLES["rejected"])
def test_deliverable_address_rejects_at_construction(url: str) -> None:
    with pytest.raises(ValidationError):
        DeliveredUrl(url=url, caption=None)


@pytest.mark.usefixtures("backend_env")
def test_a_malformed_address_fails_only_that_item(workspace: Path) -> None:
    """回归：畸形地址曾在 urlsplit 或计算判等 key 时抛出，整批交付失败、同批的合法文件也没交付。"""
    bad = ("http://[::1", "http://localhost:$PORT", "https://exa mple.com/x", "https://u:t@a.com/")
    observation = _present(workspace, ("docs/report.md", None), *((target, None) for target in bad))

    assert observation.status == "success"
    assert [item.workspace_path for item in observation.deliverables] == ["docs/report.md"]
    for target in bad:
        assert f"- {target}:" in observation.content
    assert "remove the user name or password" in observation.content


@pytest.mark.usefixtures("backend_env")
def test_file_addresses_are_delivered_as_local_files(workspace: Path) -> None:
    """file:// 换算成本机路径交付：与按路径交付是同一条记录（同一个 key），去重、现读与出口都成立。"""
    report = workspace / "docs" / "report.md"
    observation = _present(
        workspace,
        (report.as_uri(), None),
        ("docs/report.md", None),
        ("file://other-host/share/a.md", None),
    )

    assert [item.workspace_path for item in observation.deliverables] == ["docs/report.md"]
    assert (
        "- file://other-host/share/a.md: file:// addresses must point to this machine"
        in observation.content
    )


@pytest.mark.usefixtures("backend_env")
def test_images_record_their_upright_size_for_layout(workspace: Path) -> None:
    """位图记下转正后的宽高（卡片排版只看记录）；SVG 与读不出宽高的图为空。"""
    from PIL import Image

    photo = Image.new("RGB", (40, 20))
    exif = photo.getexif()
    exif[0x0112] = 6  # 顺时针转 90°：浏览器显示为 20×40
    photo.save(workspace / "photo.jpg", exif=exif)
    (workspace / "logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    (workspace / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    observation = _present(workspace, ("photo.jpg", None), ("logo.svg", None), ("broken.png", None))

    assert [(item.kind, item.width, item.height) for item in observation.deliverables] == [
        ("image", 20, 40),
        ("svg", None, None),
        ("image", None, None),
    ]


@pytest.mark.usefixtures("backend_env")
def test_nothing_presentable_is_an_error_with_a_neutral_hint(workspace: Path) -> None:
    observation = _present(workspace, ("missing.md", None), ("/dev/null", None))

    assert observation.status == "error"
    assert observation.deliverables is None
    assert "nothing could be presented" in observation.content
    assert (observation.display_data or {}).get("status_hint") == "无法呈现"


@pytest.mark.usefixtures("backend_env")
def test_same_target_is_kept_once_at_its_last_position(workspace: Path) -> None:
    """同一指向（大小写变体、链接、等价网址）只留最后一次，位置也取最后一次；硬链接是两个名字、两项。"""
    (workspace / "alias.md").symlink_to(workspace / "docs" / "report.md")
    os.link(workspace / "docs" / "report.md", workspace / "hard.md")
    variants = ["docs/report.md", "alias.md"]
    if (workspace / "DOCS").exists():
        variants.append("DOCS/REPORT.md")

    observation = _present(
        workspace,
        *[(target, None) for target in variants],
        ("http://example.com:80", None),
        ("dist", None),
        ("HTTP://EXAMPLE.COM/#top", "首页"),
        ("hard.md", None),
    )

    items = observation.deliverables
    assert [item.type for item in items] == ["file", "directory", "url", "file"]
    assert items[2].caption == "首页"
    assert items[0].key != items[3].key


def test_url_key_normalizes_only_the_documented_parts() -> None:
    assert DeliveredUrl.normalized("HTTP://LocalHost:80") == "http://localhost/"
    assert DeliveredUrl.normalized("https://a.com:443/x?q=1#frag") == "https://a.com/x?q=1"
    assert DeliveredUrl.normalized("https://a.com:8443/X") == "https://a.com:8443/X"
    assert DeliveredUrl.normalized("http://[::1]:5173/") == "http://[::1]:5173/"


def test_part_projection_carries_the_record_in_wire_shape() -> None:
    """part 上用驼峰 wire 形状并带后端算好的 key；没有记录时不写这个字段（「有才写」）。"""
    record = DeliveredFile(
        workspace_path="docs/a.md",
        absolute_path="/ws/docs/a.md",
        caption=None,
        kind="markdown",
        size=3,
        width=None,
        height=None,
    )
    fields = project_tool_result(
        ToolMessageMetadata(
            status="completed", deliverables=(record, DeliveredUrl(url="http://h", caption="c"))
        )
    )
    assert fields["deliverables"] == [
        {
            "type": "file",
            "key": "/ws/docs/a.md",
            "caption": None,
            "workspacePath": "docs/a.md",
            "absolutePath": "/ws/docs/a.md",
            "kind": "markdown",
            "size": 3,
            "width": None,
            "height": None,
        },
        {"type": "url", "key": "http://h/", "caption": "c", "url": "http://h"},
    ]
    assert "deliverables" not in project_tool_result(ToolMessageMetadata(status="completed"))


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
    for tool in (PresentDeliverablesTool(), OfferNextStepTool()):
        definition = tool.to_definition()
        assert definition.conversation_presentation == "quiet"
        assert definition.requires_user_surface
        assert definition.user_decision is False
        assert definition.name in main_agent().allowed_tools


# 目的：这批交付的说明进展示数据给卡片看；潜在缺陷：空白说明画出一行空白、超长说明撑出卡片、整批失败还留着说明。
@pytest.mark.usefixtures("backend_env")
def test_note_goes_to_the_card_display_only(workspace: Path) -> None:
    from app.tools.tool_models import PresentDeliverablesArgs
    from app.tools.tool_models.present_deliverables_args import PRESENT_DELIVERABLES_NOTE_MAX

    noted = _present(workspace, ("docs/report.md", None), note="  报告与原始数据，表已按点赞排序  ")
    assert noted.display_data == {"kind": "deliverables", "note": "报告与原始数据，表已按点赞排序"}
    assert noted.deliverables is not None
    assert _present(workspace, ("docs/report.md", None), note="   ").display_data == {
        "kind": "deliverables",
        "note": None,
    }
    # 部分失败：照常呈现通过的，说明保留。
    partial = _present(workspace, ("docs/report.md", None), ("missing.md", None), note="报告")
    assert partial.display_data == {"kind": "deliverables", "note": "报告"}
    # 全部失败：没有卡片，也就没有说明（错误态不带成功态 kind）。
    failed = _present(workspace, ("missing.md", None), note="报告")
    assert failed.status == "error"
    assert not (failed.display_data or {}).get("kind")
    # 超长在参数边界拒绝（schema 同一份上限），不截断成半句话。
    with pytest.raises(ValidationError):
        PresentDeliverablesArgs.model_validate(
            {"items": [{"target": "a", "caption": None}], "note": "长" * (PRESENT_DELIVERABLES_NOTE_MAX + 1)}
        )
