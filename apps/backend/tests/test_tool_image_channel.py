"""工具产出图片的通用通道：存图、准入、投递顺序、渲染与请求时解析。

用一个测试替身工具验证，不依赖浏览器：任何工具只要「拿到字节 → ``save_image`` →
``tool_success(images=...)``」并声明 ``returns_images``，图片就能进模型。
"""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import BaseModel, ConfigDict

import app.core.context.vision_input as vision_input
import app.core.workflow.node_helper.tool_observation_projection as projection
from app.core.tool.execute.tool_access_gate import ToolAccessGate
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import ToolCall, ToolDefinition, ToolExecutionContext
from app.core.tool.tool_registry import ToolRegistry
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.core.workflow.execution_state.tool_batch import ToolBatch
from app.core.workflow.message_queue.task_message import (
    MODEL_INPUT_BODIES,
    NoticeBody,
    TaskMessage,
    ToolImageBody,
)
from app.service.attachment.attachment_service import AttachmentService


def _png(color: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    stream = BytesIO()
    Image.new("RGB", (8, 5), color).save(stream, format="PNG")
    return stream.getvalue()


def _service(root: Path) -> AttachmentService:
    service = AttachmentService.__new__(AttachmentService)
    service._workspaces = SimpleNamespace(
        get_workspace=lambda _workspace_id: SimpleNamespace(root_path=str(root))
    )
    return service


def test_saved_tool_image_reaches_the_model_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)

    saved = service.save_image(7, _png(), "screenshot.png")
    again = service.save_image(7, _png(), "other-name.png")

    # 与上传同一套发布规则：按规范化字节去重，落在同一个工作区附件目录。
    assert again.descriptor.asset_id == saved.descriptor.asset_id
    assert saved.path == f".cosir/Attachment/{saved.descriptor.asset_id}.png"
    assert len(list((tmp_path / ".cosir" / "Attachment").glob("*.png"))) == 1

    message, metadata = ToolImageBody(
        tool_call_id="call-1", tool_name="demo_capture", image_paths=[saved.path]
    ).render()
    assert metadata.kind == "tool_image"
    assert metadata.tool_call_id == "call-1"

    monkeypatch.setattr(vision_input, "AttachmentService", lambda: service)
    [resolved] = vision_input.resolve_messages_for_model([message], workspace_id=7)
    text, image = resolved["content"]
    assert text == {
        "type": "text",
        "text": '<tool_image tool_call_id="call-1" tool_name="demo_capture" />',
    }
    url = image["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]).startswith(b"\x89PNG")


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _gate_outcome(supports_image: bool | None):
    definition = ToolDefinition(
        name="demo_capture",
        group="测试",
        description="capture",
        handler=lambda **_: None,
        args_model=_NoArgs,
        returns_images=True,
    )
    agent = (
        None
        if supports_image is None
        else SimpleNamespace(model=SimpleNamespace(supports_image=supports_image))
    )
    context = ToolExecutionContext(
        task_id=1, workspace_id=1, workspace_root=Path("."), step_id="s", run_id=1, agent=agent
    )
    gate = ToolAccessGate(ToolRegistry([definition]))
    return gate.evaluate(ToolCall("demo_capture", "{}", "call-1"), context)


# 目的：判定只在准入一处；潜在缺陷：不看图的模型拿到截图请求失败，或每个出图工具各写一遍判断。
def test_gate_rejects_image_tools_for_text_only_models() -> None:
    denied = _gate_outcome(supports_image=False)
    assert not denied.admitted
    assert denied.denial is not None
    assert denied.denial.display_data == {"status_hint": "模型不支持图片"}
    assert _gate_outcome(supports_image=True).admitted
    # 不经 Run 的直接调用（没有 Agent 配置）不做模型判断。
    assert _gate_outcome(supports_image=None).admitted


# 目的：图片紧跟它所属的那批工具结果；潜在缺陷：图片排在通知或用户输入之后，模型对不上调用。
def test_tool_images_are_merged_before_notices() -> None:
    mailbox = RunMailbox()
    mailbox.post(
        TaskMessage.of(NoticeBody(content="tools changed", notice_kind="tool_availability"))
    )
    mailbox.post(
        TaskMessage.of(ToolImageBody(tool_call_id="c", tool_name="t", image_paths=["a.png"]))
    )
    assert [mailbox.pop().kind, mailbox.pop().kind] == ["tool_image", "system_notice"]
    image = TaskMessage.of(ToolImageBody(tool_call_id="c", tool_name="t", image_paths=["a.png"]))
    assert image.project_to_transport is False
    assert ToolImageBody in MODEL_INPUT_BODIES


def test_observe_posts_images_only_for_successful_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mailbox = RunMailbox()
    monkeypatch.setattr(projection, "_runtime_context", lambda: SimpleNamespace(mailbox=mailbox))
    with_images = tool_success("demo_capture", "see image", images=["one.png", "two.png"])
    with_images.tool_call_id = "call-1"
    failed = tool_error("demo_capture", "boom", "call-2")
    failed.images = ("ignored.png",)
    plain = tool_success("read_file", "text only")

    projection.post_tool_images(ToolBatch(observations=[with_images, failed, plain]))

    posted = mailbox.pop()
    assert isinstance(posted.body, ToolImageBody)
    assert posted.body.model_dump() == {
        "tool_call_id": "call-1",
        "tool_name": "demo_capture",
        "image_paths": ["one.png", "two.png"],
    }
    assert mailbox.isEmpty()


def test_tool_batch_keeps_images_through_persistence() -> None:
    """执行状态整体落库：观察的 images 必须能随 ToolBatch 往返。"""

    observation = tool_success("demo_capture", "see image", images=["one.png"])
    restored = ToolBatch.model_validate_json(
        ToolBatch(observations=[observation]).model_dump_json()
    )
    assert restored.observations[0].images == ("one.png",)
