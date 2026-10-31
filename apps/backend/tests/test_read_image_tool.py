"""read_image：逐张校验、部分失败不整批失败、图片按上传规则发布并经工具图片通道交给模型。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import app.tools.tool_handler.read_image as read_image_module
from app.core.agents.define_agents import general_child_agent, main_agent, only_read_child_agent
from app.core.tool.schemas import TOOL_READ_IMAGE, ToolExecutionContext
from app.core.tool.schemas.model_tool_schema import strict_parameters_schema
from app.core.tool.validation.arguments import validate_tool_arguments
from app.service.attachment.attachment_service import AttachmentService
from app.service.attachment.image_normalizer import ImageNormalizationError
from app.tools.tool_handler.read_image import ReadImageTool
from app.tools.tool_models import ReadImageArgs


def _png(
    path: Path, size: tuple[int, int] = (16, 10), color: tuple[int, int, int] = (10, 120, 200)
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


def _attachments(root: Path) -> AttachmentService:
    service = AttachmentService.__new__(AttachmentService)
    service._workspaces = SimpleNamespace(
        get_workspace=lambda _workspace_id: SimpleNamespace(root_path=str(root))
    )
    return service


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    service = _attachments(root)
    monkeypatch.setattr(read_image_module, "AttachmentService", lambda: service)
    return root


def _context(root: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        task_id=7, workspace_id=1, workspace_root=root, step_id="s", run_id=1, tool_call_id="c1"
    )


def test_partial_failures_keep_the_readable_images(workspace: Path, tmp_path: Path) -> None:
    _png(workspace / "docs" / "a.png")
    outside = _png(tmp_path / "outside" / "b.png", color=(200, 10, 10))
    (workspace / "notes.txt").write_text("not an image")
    (workspace / "folder").mkdir()

    observation = ReadImageTool().execute(
        ["docs/a.png", "notes.txt", "missing.png", "folder", str(outside), "docs/a.png"],
        execution_context=_context(workspace),
    )

    assert observation.status == "success"
    # 重复路径只读一次；工作区外的绝对路径可以读。
    assert len(observation.images) == 2
    assert all(path.startswith(".cosir/Attachment/") for path in observation.images)
    assert all((workspace / path).is_file() for path in observation.images)
    lines = observation.content.splitlines()
    assert lines[0] == "read 2 image(s); they follow this result in this order:"
    assert lines[1] == "1. docs/a.png (16x10)"
    assert lines[2] == f"2. {outside} (16x10)"
    assert "not read (3):" in lines
    assert "- notes.txt: not a valid image" in lines
    assert "- missing.png: no such file" in lines
    assert "- folder: not a regular file" in lines
    display = observation.display_data
    assert display["kind"] == "image-read"
    assert [image["path"] for image in display["images"]] == ["docs/a.png", str(outside)]
    assert display["failures"] == [
        {"path": "notes.txt", "hint": "不是有效图片"},
        {"path": "missing.png", "hint": "文件不存在"},
        {"path": "folder", "hint": "不是文件"},
    ]


def test_all_failures_return_an_error_listing_each_reason(workspace: Path) -> None:
    Image.new("RGB", (4, 4)).save(workspace / "pic.gif", format="GIF")

    observation = ReadImageTool().execute(
        ["pic.gif", "/dev/zero"], execution_context=_context(workspace)
    )

    assert observation.status == "error"
    assert observation.display_data == {"status_hint": "无法读取图片"}
    assert "- pic.gif: unsupported format; only JPEG and PNG are supported" in observation.content
    assert "- /dev/zero: blocked device path" in observation.content
    assert not observation.images


def test_images_beyond_the_batch_total_are_reported_not_truncated(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _png(workspace / "a.png")
    _png(workspace / "b.png", color=(1, 2, 3))
    monkeypatch.setattr(
        read_image_module,
        "DEFAULT_IMAGE_INPUT_POLICY",
        replace(
            read_image_module.DEFAULT_IMAGE_INPUT_POLICY,
            request_total_max_bytes=first.stat().st_size + 1,
        ),
    )

    observation = ReadImageTool().execute(["a.png", "b.png"], execution_context=_context(workspace))

    assert len(observation.images) == 1
    assert (
        "- b.png: skipped: the images in this call would exceed 0 MB in total"
        in observation.content
    )
    assert observation.display_data["failures"] == [{"path": "b.png", "hint": "超出总大小"}]


def test_import_is_idempotent_and_leaves_the_source_untouched(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    source = _png(tmp_path / "src.png")
    before = source.read_bytes()
    service = _attachments(root)

    first = service.import_image(1, source)
    again = service.import_image(1, source)
    # 导入已发布的附件本身也得到同一个 asset。
    republished = service.import_image(1, root / first.path)

    assert first == again
    assert republished.descriptor.asset_id == first.descriptor.asset_id
    assert source.read_bytes() == before
    assert list((root / ".cosir" / "Attachment" / ".uploading").iterdir()) == []
    with pytest.raises(ImageNormalizationError) as missing:
        service.import_image(1, tmp_path / "nope.png")
    assert missing.value.code == "ATTACHMENT_NOT_FOUND"


def test_definition_schema_and_agent_exposure() -> None:
    definition = ReadImageTool().to_definition()
    assert definition.returns_images is True
    assert definition.execution_mode == "thread"
    schema = strict_parameters_schema(ReadImageArgs)
    assert schema["properties"]["paths"]["maxItems"] == 10
    assert not validate_tool_arguments(
        {"paths": []}, definition.parameters_schema, ReadImageArgs
    ).ok
    for profile in (main_agent(), general_child_agent(), only_read_child_agent()):
        assert TOOL_READ_IMAGE in profile.allowed_tools
