from pathlib import Path

import pytest
from PIL import Image

from app.assistant_transport.event import UserInputAppendedEvent
from app.assistant_transport.projection.event_projection import plan_event
from app.assistant_transport.service.conversation_task_state_service import _apply_mutation
from app.assistant_transport.state.conversation_state_snapshot import empty_snapshot
from app.core.events import RunInitializedEvent
from app.service.attachment.image_normalizer import (
    ImageNormalizationError,
    normalize_image,
)
from app.service.attachment.image_policy import DEFAULT_IMAGE_INPUT_POLICY


def test_normalize_rejects_unsupported_bmp(tmp_path: Path) -> None:
    source = tmp_path / "source.bmp"
    target = tmp_path / ".normalized.part"
    Image.new("RGB", (8, 6), (20, 40, 60)).save(source, format="BMP")

    with pytest.raises(ImageNormalizationError) as error:
        normalize_image(source, target)

    assert error.value.code == "ATTACHMENT_TYPE_UNSUPPORTED"


def test_normalize_resizes_image_by_longest_side(tmp_path: Path) -> None:
    source = tmp_path / "source.jpeg"
    target = tmp_path / ".normalized.part"
    Image.new("RGB", (10_000, 5_000), (20, 40, 60)).save(source, format="JPEG")

    result = normalize_image(source, target)

    assert result.target_format == "jpeg"
    assert result.width == DEFAULT_IMAGE_INPUT_POLICY.max_image_side_px
    assert result.height == 4_096
    with Image.open(target) as output:
        assert output.size == (DEFAULT_IMAGE_INPUT_POLICY.max_image_side_px, 4_096)


def test_normalize_preserves_already_supported_png(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    target = tmp_path / ".normalized.part"
    Image.new("RGBA", (4, 3), (20, 40, 60, 120)).save(source, format="PNG")

    result = normalize_image(source, target)

    assert result.source_format == "png"
    assert result.target_format == "png"
    assert target.read_bytes() == source.read_bytes()


def test_normalize_uses_unified_image_policy_as_format_source() -> None:
    assert DEFAULT_IMAGE_INPUT_POLICY.supported_formats == {
        "jpeg",
        "png",
    }


def _apply_planned(event, snapshot):
    for mutation in plan_event(event, snapshot):
        _apply_mutation(snapshot, mutation)
    return snapshot


def test_run_initialized_has_no_messages_and_input_event_projects_image() -> None:
    """Run 骨架不预置 user 消息；用户输入事件把图片 locator 原样投影为独立 user 消息。"""

    snapshot = _apply_planned(RunInitializedEvent(task_id=1, run_id=1), empty_snapshot())
    assert snapshot["runs"][0]["messages"] == []

    image_part = {"type": "image", "image": "cosir-attachment://" + "1" * 64}
    snapshot = _apply_planned(
        UserInputAppendedEvent(task_id=1, run_id=1, message_id="user-1", parts=[image_part]),
        snapshot,
    )
    user_message, assistant_message = snapshot["runs"][0]["messages"]
    assert user_message["role"] == "user"
    assert user_message["parts"] == [image_part]
    assert assistant_message["role"] == "assistant"
    assert assistant_message["parts"] == []


def test_normalizer_does_not_follow_existing_target_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source.bmp"
    outside = tmp_path / "outside.jpg"
    target = tmp_path / "normalized.jpeg"
    Image.new("RGB", (2, 2), "white").save(source, format="BMP")
    outside.write_bytes(b"outside")
    try:
        target.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises(ImageNormalizationError):
        normalize_image(source, target)

    assert outside.read_bytes() == b"outside"
