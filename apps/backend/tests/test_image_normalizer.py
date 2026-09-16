from pathlib import Path

import pytest
from PIL import Image

from app.assistant_transport.event import RunInitializedEvent, UserInputAppendedEvent
from app.assistant_transport.state.conversation_state_snapshot import empty_snapshot
from app.core.llm_provider.capability.model_capability import ModelCapability
from app.service.attachment.attachment_service import collect_workspace_orphans
from app.service.attachment.image_normalizer import (
    ImageNormalizationError,
    normalize_image,
)

VISION_MODEL = "deepseek-flash"


def test_normalize_static_image_to_model_supported_format(tmp_path: Path) -> None:
    source = tmp_path / "source.bmp"
    target = tmp_path / ".normalized.part"
    Image.new("RGB", (8, 6), (20, 40, 60)).save(source, format="BMP")

    result = normalize_image(source, target, VISION_MODEL)

    assert result.source_format == "bmp"
    assert result.target_format == "jpeg"
    assert result.content_type == "image/jpeg"
    assert result.path == target
    with Image.open(target) as output:
        assert output.format == "JPEG"
        assert output.size == (8, 6)


def test_normalize_preserves_already_supported_png(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    target = tmp_path / ".normalized.part"
    Image.new("RGBA", (4, 3), (20, 40, 60, 120)).save(source, format="PNG")

    result = normalize_image(source, target, VISION_MODEL)

    assert result.source_format == "png"
    assert result.target_format == "png"
    assert target.read_bytes() == source.read_bytes()


def test_normalize_rejects_image_for_non_vision_model(tmp_path: Path) -> None:
    source = tmp_path / "source.bmp"
    target = tmp_path / ".normalized.part"
    Image.new("RGB", (2, 2), "white").save(source, format="BMP")
    non_vision_model = "deepseek-v4-pro"

    with pytest.raises(ImageNormalizationError) as error:
        normalize_image(source, target, non_vision_model)

    assert error.value.code == "VISION_NOT_SUPPORTED"
    assert not target.exists()


def test_normalize_uses_model_capability_as_format_source() -> None:
    capability = ModelCapability.get_capability(VISION_MODEL)

    assert set(capability.image_limit.supported_formats) == {"jpeg", "png", "gif", "webp"}


def test_run_initialized_creates_empty_user_skeleton_and_input_event_projects_image() -> None:
    initialized = RunInitializedEvent(task_id=1, run_id=1)
    initialized_snapshot = initialized.plan(empty_snapshot())[0].value
    assert initialized_snapshot["messages"][0]["parts"] == []

    input_event = UserInputAppendedEvent(
        task_id=1,
        run_id=1,
        parts=[{"type": "image", "image": "cosir-attachment://" + "1" * 64}],
    )
    input_snapshot = dict(initialized_snapshot)
    input_snapshot["runs"] = [initialized_snapshot]
    mutations = input_event.plan(input_snapshot)
    assert mutations[0].value == [
        {"type": "image", "image": "cosir-attachment://" + "1" * 64}
    ]


def test_collect_workspace_orphans_keeps_referenced_asset_family(tmp_path: Path) -> None:
    attachment_dir = tmp_path / ".cosir" / "Attachment"
    staging_dir = attachment_dir / ".uploading"
    staging_dir.mkdir(parents=True)
    keep_id = "1" * 64
    orphan_id = "2" * 64
    (attachment_dir / f"{keep_id}.jpeg").write_bytes(b"keep")
    (attachment_dir / f"{keep_id}.source.bmp").write_bytes(b"keep-source")
    (attachment_dir / f"{orphan_id}.jpeg").write_bytes(b"orphan")
    (staging_dir / f"{orphan_id}.png").write_bytes(b"orphan-staging")

    removed = collect_workspace_orphans(
        tmp_path,
        [f".cosir/Attachment/{keep_id}.jpeg"],
    )

    assert removed == 2
    assert (attachment_dir / f"{keep_id}.jpeg").exists()
    assert (attachment_dir / f"{keep_id}.source.bmp").exists()
    assert not (attachment_dir / f"{orphan_id}.jpeg").exists()
    assert not (staging_dir / f"{orphan_id}.png").exists()


def test_collect_workspace_orphans_rejects_attachment_symlink(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    attachment_dir = tmp_path / ".cosir" / "Attachment"
    attachment_dir.parent.mkdir()
    try:
        attachment_dir.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises(ImageNormalizationError) as error:
        collect_workspace_orphans(tmp_path, [])

    assert error.value.code == "ATTACHMENT_STORAGE_UNAVAILABLE"


def test_collect_workspace_orphans_rejects_uploading_symlink(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    attachment_dir = tmp_path / ".cosir" / "Attachment"
    attachment_dir.mkdir(parents=True)
    try:
        (attachment_dir / ".uploading").symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises(ImageNormalizationError) as error:
        collect_workspace_orphans(tmp_path, [])

    assert error.value.code == "ATTACHMENT_STORAGE_UNAVAILABLE"


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
        normalize_image(source, target, VISION_MODEL)

    assert outside.read_bytes() == b"outside"
