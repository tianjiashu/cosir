"""``resolve_messages_for_model`` 的图片引用解析契约。

该链路此前完全没有用例覆盖，三处缺陷因此长期存在：把 langchain 的只读 property
``content_blocks`` 当可写字段赋值、循环的「本条消息是否需要解析」判据写反（带图消息被跳过、
纯文本消息被误处理）、转换结果缺 ``mime_type``（provider 适配器直接抛错）。本文件按「能被
provider 消费」这一外部契约钉住该行为。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from app.core.workflows import vision_input
from app.models.errors.llm_provider_exceptions import VisionImageError

_IMAGE_REF = {"type": "image", "file_id": "attachments/a.png"}
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


def _human(*blocks: dict[str, str]) -> HumanMessage:
    """按 canonical 形状构造人类消息（整块复制，避免用例间共享可变对象）。"""

    return HumanMessage(content=[dict(block) for block in blocks])


def test_mixed_history_resolves_only_the_image_message(
    attachment: _FakeAttachmentService,
) -> None:
    """同一次请求里既有纯文本 user 消息、又有带图 user 消息时，只转换带图那条且不崩。"""

    plain = _human({"type": "text", "text": "先聊点别的"})
    with_image = _human(_TEXT, _IMAGE_REF)

    resolved = vision_input.resolve_messages_for_model(
        [SystemMessage(content="sys"), plain, with_image], workspace_id=7
    )

    assert attachment.calls == [(7, "attachments/a.png")]
    # 入参不被改写；纯文本消息原样保留。
    assert plain.content == [{"type": "text", "text": "先聊点别的"}]
    assert resolved[1].content == [{"type": "text", "text": "先聊点别的"}]
    # 带图消息被就地替换为已解析的图片块。
    image_block = resolved[2].content[1]
    assert image_block["type"] == "image"
    assert image_block["mime_type"] == "image/png"
    assert image_block["base64"] == "iVBORw0KGgpub3QtYS1yZWFsLXBuZw=="
    assert "file_id" not in image_block


def test_resolved_block_converts_to_provider_image_url(
    attachment: _FakeAttachmentService,
) -> None:
    """解析结果必须能被 provider 适配器转成 ``image_url`` data URL（缺 ``mime_type`` 会抛错）。"""

    # 用适配器自身做断言：它正是生产路径构造 payload 的入口，能同时钉住块形状与 MIME。
    from langchain_openai.chat_models.base import _convert_message_to_dict

    resolved = vision_input.resolve_messages_for_model(
        [_human(_TEXT, _IMAGE_REF)], workspace_id=1
    )

    payload = _convert_message_to_dict(resolved[0])

    image_block = payload["content"][1]
    assert image_block["type"] == "image_url"
    assert image_block["image_url"]["url"].startswith("data:image/png;base64,")


def test_unresolved_reference_without_path_is_rejected(
    attachment: _FakeAttachmentService,
) -> None:
    """``file_id`` 为空时必须显式失败，不得把坏块放行给 provider。"""

    with pytest.raises(VisionImageError):
        vision_input.resolve_messages_for_model(
            [_human(_TEXT, {"type": "image", "file_id": ""})], workspace_id=1
        )

    assert attachment.calls == []


def test_already_resolved_image_is_left_untouched(
    attachment: _FakeAttachmentService,
) -> None:
    """已解析（含 ``base64``）的图片块不得被重复编码，也不触发附件读取。"""

    resolved_block = {"type": "image", "base64": "AAA", "mime_type": "image/png"}

    resolved = vision_input.resolve_messages_for_model(
        [_human(_TEXT, resolved_block)], workspace_id=1
    )

    assert resolved[0].content[1] == resolved_block
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
