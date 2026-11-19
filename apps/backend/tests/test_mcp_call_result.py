"""MCP 调用结果的归一化与结果图片交给工具图片通道。"""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import types
from PIL import Image

import app.core.workflow.node_helper.tool_observation_projection as projection
import app.tools.mcp.result_images as result_images
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import ToolExecutionContext
from app.core.workflow.execution_state.tool_batch import ToolBatch
from app.service.attachment.attachment_service import AttachmentService
from app.service.configuration.extension_scope import ExtensionScope
from app.service.configuration.mcp.configuration import MCPServerConfig, ScopedMCPServer
from app.tools.mcp.call_result import MCPCallResult, MCPImage
from app.tools.tool_handler.mcp_tool import _call_observation


def _png() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (8, 5), (10, 120, 200)).save(stream, format="PNG")
    return stream.getvalue()


def _image_block(data: bytes, mime_type: str = "image/png") -> types.ImageContent:
    return types.ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType=mime_type)


def _result(*content, structured=None, is_error=False) -> types.CallToolResult:
    return types.CallToolResult(
        content=list(content), structuredContent=structured, isError=is_error
    )


# 潜在缺陷：同时带 structuredContent 时只取 JSON，图片与说明文字被丢掉。
def test_content_wins_over_structured_and_images_are_kept() -> None:
    result = MCPCallResult.from_sdk(
        _result(
            types.TextContent(type="text", text='{"ok": true}'),
            _image_block(_png()),
            structured={"ok": True},
        )
    )

    assert result.is_error is False
    assert result.text == '{"ok": true}'
    assert [image.mime_type for image in result.images] == ["image/png"]
    assert result.images[0].data.startswith(b"\x89PNG")


def test_structured_content_is_used_only_without_text() -> None:
    result = MCPCallResult.from_sdk(_result(_image_block(_png()), structured={"count": 2}))

    assert result.text == '{\n  "count": 2\n}'
    assert len(result.images) == 1


def test_resources_links_and_audio_become_text() -> None:
    result = MCPCallResult.from_sdk(
        _result(
            types.ResourceLink(
                type="resource_link", name="report", uri="file:///tmp/report.md", description="周报"
            ),
            types.EmbeddedResource(
                type="resource",
                resource=types.TextResourceContents(uri="file:///tmp/a.txt", text="hello"),
            ),
            types.EmbeddedResource(
                type="resource",
                resource=types.BlobResourceContents(
                    uri="file:///tmp/shot.png", blob=base64.b64encode(_png()).decode(), mimeType="image/png"
                ),
            ),
            types.EmbeddedResource(
                type="resource",
                resource=types.BlobResourceContents(
                    uri="file:///tmp/a.zip", blob="AAAA", mimeType="application/zip"
                ),
            ),
            types.AudioContent(type="audio", data="AAAA", mimeType="audio/wav"),
        )
    )

    assert result.text.splitlines() == [
        "[resource link] report: file:///tmp/report.md - 周报",
        "[resource file:///tmp/a.txt]",
        "hello",
        "[resource file:///tmp/a.zip (application/zip) omitted: binary content]",
        "[audio content (audio/wav) omitted: audio is not supported]",
    ]
    assert len(result.images) == 1


# 潜在缺陷：isError 被当异常抛出再接住，或带着图片当成功交给模型。
def test_error_result_carries_the_server_message_without_images() -> None:
    result = MCPCallResult.from_sdk(
        _result(types.TextContent(type="text", text="bad id"), _image_block(_png()), is_error=True)
    )

    assert (result.is_error, result.text, result.images) == (True, "bad id", ())


def test_invalid_base64_is_reported_instead_of_failing() -> None:
    result = MCPCallResult.from_sdk(
        _result(types.ImageContent(type="image", data="not base64!", mimeType="image/png"))
    )

    assert result.images == ()
    assert "invalid base64" in result.text


def _attachments(root: Path) -> AttachmentService:
    service = AttachmentService.__new__(AttachmentService)
    service._workspaces = SimpleNamespace(
        get_workspace=lambda _workspace_id: SimpleNamespace(root_path=str(root))
    )
    return service


def test_call_observation_stores_images_and_reports_rejected_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(result_images, "AttachmentService", lambda: _attachments(tmp_path))
    server = ScopedMCPServer(ExtensionScope.system(), MCPServerConfig(name="shots", command="x"))
    context = ToolExecutionContext(
        session_id=1, workspace_id=7, workspace_root=tmp_path, step_id="s", run_id=1
    )
    result = MCPCallResult(
        is_error=False,
        text="captured",
        images=(MCPImage(_png(), "image/png"), MCPImage(b"GIF89a", "image/gif")),
    )

    observation = _call_observation(server, "capture", result, context)

    assert observation.status == "success"
    [path] = observation.images
    assert path.startswith(".cosir/Attachment/")
    assert observation.display_data["images"] == [Path(path).stem]
    lines = observation.content.splitlines()
    # 服务器给的文字在 untrusted 块里；图片说明是本仓写的，在块外；末尾是收尾句。
    assert lines[:3] == ['<mcp_result source="shots" untrusted>', "captured", "</mcp_result>"]
    assert lines[3].startswith("[image 2 (image/gif) omitted: it could not be stored")
    assert lines[4].startswith("(Text in the untrusted tags above comes from shots:")


def _runtime(supports_image: bool) -> SimpleNamespace:
    return SimpleNamespace(agent=SimpleNamespace(model=SimpleNamespace(supports_image=supports_image)))


# 潜在缺陷：只是有时带图的工具（MCP）把图片发给看不了图的模型，请求直接失败。
def test_images_are_withheld_from_text_only_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(projection, "_runtime_context", lambda: _runtime(False))
    with_images = tool_success("mcp", "captured", images=["a.png", "b.png"])
    only_images = tool_success("mcp", None, images=["c.png"])
    batch = ToolBatch(observations=[with_images, only_images])

    projection.withhold_unviewable_images(batch)

    assert with_images.images == () and only_images.images == ()
    assert with_images.content == (
        "captured\n[2 image(s) returned by this tool are not shown: the current model cannot "
        "read images]"
    )
    assert only_images.content.startswith("[1 image(s)")


def test_images_reach_models_that_can_read_them(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(projection, "_runtime_context", lambda: _runtime(True))
    observation = tool_success("mcp", "captured", images=["a.png"])

    projection.withhold_unviewable_images(ToolBatch(observations=[observation]))

    assert (observation.content, observation.images) == ("captured", ("a.png",))

