"""SDK image_url 中的本机附件定位符只在模型请求副本中解析。"""

from __future__ import annotations

from pathlib import Path

import pytest
from openai.types.chat import ChatCompletionUserMessageParam

import app.core.workflow.vision_input as vision_input
from app.models.errors.llm_provider_exceptions import VisionImageError

_IMAGE_REF = {"type": "image_url", "image_url": {"url": "cosir-attachment://attachments/a.png"}}
_TEXT = {"type": "text", "text": "看看这张图"}


class _StoredImage:
    """``StoredImage`` 值对象的最小形状（``path`` / ``content_type`` / ``byte_size``）。"""

    def __init__(self, path: Path, content_type: str) -> None:
        self.path = path
        self.content_type = content_type
        self.byte_size = path.stat().st_size


class _FakeAttachmentService:
    """只记录调用并返回预置图片句柄，不碰真实 workspace 目录。"""

    def __init__(self, path: Path, content_type: str = "image/png") -> None:
        self._path = path
        self._content_type = content_type
        self.calls: list[tuple[int, str]] = []

    def resolve_for_model(self, workspace_id: int, image_path: str) -> _StoredImage:
        self.calls.append((workspace_id, image_path))
        return _StoredImage(self._path, self._content_type)


@pytest.fixture()
def attachment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeAttachmentService:
    image = tmp_path / "a.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-png")
    service = _FakeAttachmentService(image)
    monkeypatch.setattr(vision_input, "AttachmentService", lambda: service)
    return service


def _human(*blocks: dict[str, str]) -> ChatCompletionUserMessageParam:
    """按 canonical 形状构造人类消息（整块复制，避免用例间共享可变对象）。"""

    return {"role": "user", "content": [dict(block) for block in blocks]}


def test_mixed_history_resolves_only_the_image_message(
    attachment: _FakeAttachmentService,
) -> None:
    """同一次请求里既有纯文本 user 消息、又有带图 user 消息时，只转换带图那条且不崩。"""

    plain = _human({"type": "text", "text": "先聊点别的"})
    with_image = _human(_TEXT, _IMAGE_REF)

    resolved = vision_input.resolve_messages_for_model(
        [{"role": "system", "content": "sys"}, plain, with_image], workspace_id=7
    )

    assert attachment.calls == [(7, "attachments/a.png")]
    # 入参不被改写；纯文本消息原样保留。
    assert plain.get("content") == [{"type": "text", "text": "先聊点别的"}]
    assert resolved[1].get("content") == [{"type": "text", "text": "先聊点别的"}]
    # 带图消息被就地替换为已解析的图片块。
    image_block = resolved[2].get("content")[1]
    assert image_block["type"] == "image_url"
    assert (
        image_block["image_url"]["url"] == "data:image/png;base64,iVBORw0KGgpub3QtYS1yZWFsLXBuZw=="
    )
    assert "file_id" not in image_block


def test_resolved_block_converts_to_provider_image_url(
    attachment: _FakeAttachmentService,
) -> None:
    """解析结果直接满足 SDK 消息格式，无额外 provider 消息转换。"""

    # 通过 SDK 类型校验，确认请求副本的块形状可直接上行。
    from app.core.llm_provider.message_codec import validate_message

    resolved = vision_input.resolve_messages_for_model([_human(_TEXT, _IMAGE_REF)], workspace_id=1)

    payload = validate_message(resolved[0])

    image_block = payload["content"][1]
    assert image_block["type"] == "image_url"
    assert image_block["image_url"]["url"].startswith("data:image/png;base64,")


def test_attachment_read_failure_propagates(attachment, monkeypatch):
    def reject(*args):
        raise VisionImageError("附件不可用")

    monkeypatch.setattr(attachment, "resolve_for_model", reject)
    with pytest.raises(VisionImageError):
        vision_input.resolve_messages_for_model([_human(_TEXT, _IMAGE_REF)], workspace_id=1)


def test_already_resolved_image_is_left_untouched(
    attachment: _FakeAttachmentService,
) -> None:
    """已解析（含 ``base64``）的图片块不得被重复编码，也不触发附件读取。"""

    resolved_block = {
        "type": "image_url",
        "image_url": {"url": "https://example.invalid/image.png"},
    }

    resolved = vision_input.resolve_messages_for_model(
        [_human(_TEXT, resolved_block)], workspace_id=1
    )

    assert resolved[0].get("content")[1] == resolved_block
    assert attachment.calls == []


def test_no_image_reference_returns_deep_copy(
    attachment: _FakeAttachmentService,
) -> None:
    """没有图片引用时返回深拷贝且不读附件（早退路径）。"""

    source = _human({"type": "text", "text": "纯文本"})

    resolved = vision_input.resolve_messages_for_model([source], workspace_id=1)

    assert resolved == [source]
    assert resolved[0] is not source
    assert attachment.calls == []
