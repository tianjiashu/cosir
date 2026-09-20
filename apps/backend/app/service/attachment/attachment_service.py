"""Workspace ``.cosir/Attachment`` image storage.

The file bytes are the source of truth.  The lowercase SHA-256 digest of the
uploaded bytes is both the asset identifier and the filename stem; no
attachment database table is involved.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from mimetypes import guess_type
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from app.config.logging.logger import log
from app.service import depends as service_depends
from app.service.attachment.image_normalizer import (
    ImageNormalizationError,
    NormalizedImage,
    normalize_image,
)
from app.utils.cosir_paths import (
    workspace_attachment_dir,
    workspace_attachment_staging_dir,
    workspace_cosir_dir,
)

_ASSET_ID = re.compile(r"^[0-9a-f]{64}$")
_ASSET_FILE = re.compile(
    r"^(?P<asset_id>[0-9a-f]{64})(?:\.source)?\.(?P<extension>[A-Za-z0-9]+)$"
)
_MAX_UPLOAD_BYTES = 32 * 1024 * 1024
_IMAGE_FORMATS = {"jpeg", "png", "gif", "webp", "bmp", "tiff"}


def _normal_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = os.lstat(path).st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        return path.is_symlink()
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _assert_real_directory(path: Path, *, create: bool) -> Path:
    """Reject workspace-controlled links before touching the attachment tree."""

    if path.exists() or _is_reparse_point(path):
        if _is_reparse_point(path) or _normal_path(path.resolve(strict=False)) != _normal_path(path):
            raise ImageNormalizationError("ATTACHMENT_STORAGE_UNAVAILABLE", "附件存储目录不可用")
        if not path.is_dir():
            raise ImageNormalizationError("ATTACHMENT_STORAGE_UNAVAILABLE", "附件存储目录不可用")
        return path
    if not create:
        return path
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ImageNormalizationError("ATTACHMENT_STORAGE_UNAVAILABLE", "附件存储目录不可用") from exc
    if _is_reparse_point(path) or _normal_path(path.resolve(strict=False)) != _normal_path(path):
        raise ImageNormalizationError("ATTACHMENT_STORAGE_UNAVAILABLE", "附件存储目录不可用")
    return path


def _attachment_directory(root_path: str | Path, *, create: bool) -> Path:
    """Return the real workspace attachment directory."""

    root = Path(root_path).resolve()
    cosir = workspace_cosir_dir(root)
    directory = workspace_attachment_dir(root)
    staging = workspace_attachment_staging_dir(root)
    if create:
        _assert_real_directory(cosir, create=True)
        _assert_real_directory(directory, create=True)
        _assert_real_directory(staging, create=True)
    else:
        for candidate in (cosir, directory, staging):
            if candidate.exists() or _is_reparse_point(candidate):
                _assert_real_directory(candidate, create=False)
    return directory


def _asset_id_from_name(name: str) -> str | None:
    match = _ASSET_FILE.fullmatch(name)
    return match.group("asset_id") if match else None


def collect_workspace_orphans(root_path: str | Path, referenced_paths: Iterable[str]) -> int:
    """Remove unreferenced hash-named files from one workspace attachment tree."""

    directory = _attachment_directory(root_path, create=False)
    if not directory.is_dir():
        return 0
    referenced_ids = {
        asset_id
        for path in referenced_paths
        if (asset_id := _asset_id_from_name(Path(str(path)).name)) is not None
    }
    removed = 0
    for candidate_dir in (directory, workspace_attachment_staging_dir(root_path)):
        if not candidate_dir.is_dir() or _is_reparse_point(candidate_dir):
            continue
        for candidate in candidate_dir.iterdir():
            asset_id = _asset_id_from_name(candidate.name)
            if asset_id is None or asset_id in referenced_ids or _is_reparse_point(candidate):
                continue
            if candidate.is_file():
                candidate.unlink()
                removed += 1
    if removed:
        log.info(
            "attachment_orphans_collected",
            extra={"msg": "已清理未被 Run 引用的图片附件", "data": {"removed": removed}},
        )
    return removed


@dataclass(frozen=True, slots=True)
class AttachmentDescriptor:
    """Metadata returned by the image upload/content API."""

    asset_id: str
    name: str
    content_type: str
    byte_size: int
    width: int
    height: int
    locator: str
    status: str


@dataclass(frozen=True, slots=True)
class _TaskAttachmentDirs:
    """一次 task 所属 workspace 的根目录与附件落盘目录（仅本模块内部使用）。"""

    workspace_root: Path
    attachment_dir: Path


class AttachmentService:
    """Own local image bytes, deduplicated by their SHA-256 digest.

    The service performs workspace ownership and reparse-point checks, writes
    bytes atomically, normalizes images for a model, and checks Run references
    before deletion. It does not persist attachment metadata in SQLite.
    """

    def __init__(self) -> None:
        self._tasks = service_depends.get_task_service()
        self._workspaces = service_depends.get_workspace_service()
        self._runs = service_depends.get_conversation_run_state_service()

    def _dirs(self, task_id: int) -> _TaskAttachmentDirs:
        """返回该 task 所属 workspace 的根目录与附件落盘目录。

        ``workspace_root`` 直接取自 workspace 记录，**不**由附件目录反推——避免把 ``.cosir``
        的目录层级知识散落到本模块（层级规则由 ``app.utils.cosir_paths`` 独占）。

        参数:
            task_id: 任务标识。

        返回:
            ``_TaskAttachmentDirs``：``workspace_root`` 为解析后的 workspace 根，
            ``attachment_dir`` 为 ``<root>/.cosir/Attachment``。

        异常:
            KeyError: task 或 workspace 不存在（由依赖 service 抛出）。
            ImageNormalizationError: 附件目录不可用（reparse point / 创建失败）。

        副作用:
            可能按需创建 ``<root>/.cosir``、``<root>/.cosir/Attachment`` 与
            ``<root>/.cosir/Attachment/.uploading``。
        """

        task = self._tasks.get_task(task_id)
        workspace = self._workspaces.get_workspace(task.workspace_id)
        return _TaskAttachmentDirs(
            workspace_root=Path(workspace.root_path).resolve(),
            attachment_dir=_attachment_directory(workspace.root_path, create=True),
        )

    @staticmethod
    def _safe_asset_id(asset_id: str) -> str:
        if not _ASSET_ID.fullmatch(asset_id):
            raise ImageNormalizationError("ATTACHMENT_NOT_FOUND", "附件不存在")
        return asset_id

    @staticmethod
    def _inside(directory: Path, candidate: Path) -> Path:
        resolved_dir = directory.resolve()
        resolved = candidate.resolve()
        if resolved != resolved_dir and resolved_dir not in resolved.parents:
            raise ImageNormalizationError("ATTACHMENT_NOT_OWNED", "附件不属于当前工作区")
        return resolved

    def _files(self, directory: Path, asset_id: str) -> list[Path]:
        self._safe_asset_id(asset_id)
        candidates = [
            *directory.glob(f".uploading/{asset_id}.*"),
            *directory.glob(f"{asset_id}.*"),
        ]
        files: list[Path] = []
        for path in candidates:
            if _is_reparse_point(path):
                raise ImageNormalizationError("ATTACHMENT_STORAGE_UNAVAILABLE", "附件存储目录不可用")
            if path.is_file() and _asset_id_from_name(path.name) == asset_id:
                files.append(self._inside(directory, path))
        return files

    @staticmethod
    def _descriptor(asset_id: str, name: str, content_type: str, byte_size: int, width: int, height: int, status: str) -> AttachmentDescriptor:
        return AttachmentDescriptor(
            asset_id=asset_id,
            name=name,
            content_type=content_type,
            byte_size=byte_size,
            width=width,
            height=height,
            locator=f"cosir-attachment://{asset_id}",
            status=status,
        )

    def get_descriptor(self, task_id: int, asset_id: str, *, expected_kind: str | None = None) -> AttachmentDescriptor:
        """Return metadata for an existing image identified by its digest."""

        if expected_kind not in (None, "image"):
            raise ImageNormalizationError("ATTACHMENT_TYPE_UNSUPPORTED", "附件类型不匹配")
        safe_asset_id = self._safe_asset_id(asset_id)
        directory = self._dirs(task_id).attachment_dir
        files = self._files(directory, safe_asset_id)
        if not files:
            raise ImageNormalizationError("ATTACHMENT_NOT_FOUND", "附件不存在")
        source = next((path for path in files if ".source." not in path.name), files[0])
        return self._descriptor(
            safe_asset_id,
            source.name,
            guess_type(source.name)[0] or "application/octet-stream",
            source.stat().st_size,
            0,
            0,
            "ready" if ".uploading" not in source.parts else "staged",
        )

    async def upload(self, task_id: int, file: UploadFile) -> AttachmentDescriptor:
        """Stream an image, hash its exact bytes, and atomically deduplicate it."""

        dirs = self._dirs(task_id)
        directory = dirs.attachment_dir
        staging_dir = workspace_attachment_staging_dir(dirs.workspace_root)
        _assert_real_directory(staging_dir, create=True)
        temp_path = staging_dir / f".{uuid4().hex}.part"
        total = 0
        digest = hashlib.sha256()
        try:
            with temp_path.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > _MAX_UPLOAD_BYTES:
                        raise ImageNormalizationError("ATTACHMENT_FILE_TOO_LARGE", "附件文件过大")
                    output.write(chunk)
                    digest.update(chunk)

            from PIL import Image

            try:
                with Image.open(temp_path) as image:
                    image.verify()
                with Image.open(temp_path) as image:
                    source_format = (image.format or "").lower()
                    width, height = image.size
            except Exception as exc:
                raise ImageNormalizationError("ATTACHMENT_IMAGE_INVALID", "上传内容不是有效图片") from exc
            if source_format not in _IMAGE_FORMATS:
                raise ImageNormalizationError("ATTACHMENT_TYPE_UNSUPPORTED", "暂不支持该图片格式")

            asset_id = digest.hexdigest()
            source_extension = ".jpeg" if source_format == "jpeg" else f".{source_format}"
            staged_path = staging_dir / f"{asset_id}{source_extension}"
            try:
                # Hard-link is the no-replace claim: concurrent uploads of the
                # same digest leave exactly one staged file on disk.
                os.link(temp_path, staged_path)
            except FileExistsError:
                pass
            finally:
                temp_path.unlink(missing_ok=True)
            files = self._files(directory, asset_id)
            if not files:
                raise ImageNormalizationError("ATTACHMENT_STORAGE_UNAVAILABLE", "附件存储目录不可用")
            descriptor = self._descriptor(
                asset_id,
                file.filename or f"{asset_id}{source_extension}",
                f"image/{'jpeg' if source_format == 'jpeg' else source_format}",
                total,
                width,
                height,
                "ready" if any(".uploading" not in path.parts for path in files) else "staged",
            )
            log.info(
                "attachment_uploaded",
                extra={"msg": "图片附件已按 SHA-256 保存", "data": {"task_id": task_id, "asset_id": asset_id}},
            )
            return descriptor
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            raise ImageNormalizationError("ATTACHMENT_STORAGE_UNAVAILABLE", "附件存储目录不可用") from exc
        finally:
            await file.close()

    def finalize(self, task_id: int, asset_id: str, model_name: str) -> NormalizedImage:
        """Normalize a digest-named image and return its workspace-relative path."""

        directory = self._dirs(task_id).attachment_dir
        asset_id = self._safe_asset_id(asset_id)
        files = self._files(directory, asset_id)
        source = next((path for path in files if ".source." in path.name), None)
        source = source or next((path for path in files if ".uploading" in path.parts), None)
        source = source or next((path for path in files if ".source." not in path.name), None)
        if source is None:
            raise ImageNormalizationError("ATTACHMENT_NOT_FOUND", "附件不存在")

        target_tmp = directory / f".{asset_id}.{uuid4().hex}.normalized.part"
        try:
            normalized = normalize_image(source, target_tmp, model_name)
            target = directory / f"{asset_id}.{normalized.target_format}"
            if target.exists() and not _is_reparse_point(target):
                target_tmp.unlink(missing_ok=True)
            else:
                os.replace(target_tmp, target)
            if source.parent.name == ".uploading":
                if normalized.source_format != normalized.target_format:
                    os.replace(source, directory / f"{asset_id}.source.{normalized.source_format}")
                else:
                    source.unlink(missing_ok=True)
            return NormalizedImage(
                source_format=normalized.source_format,
                target_format=normalized.target_format,
                content_type=normalized.content_type,
                path=target,
                width=normalized.width,
                height=normalized.height,
                byte_size=normalized.byte_size,
            )
        except OSError as exc:
            raise ImageNormalizationError("ATTACHMENT_STORAGE_UNAVAILABLE", "附件存储目录不可用") from exc
        finally:
            target_tmp.unlink(missing_ok=True)

    def resolve_content(self, task_id: int, asset_id: str, *, expected_kind: str | None = None) -> tuple[Path, str]:
        """Return an existing image file and its MIME type for local content reads."""

        descriptor = self.get_descriptor(task_id, asset_id, expected_kind=expected_kind)
        directory = self._dirs(task_id).attachment_dir
        files = self._files(directory, descriptor.asset_id)
        path = next((item for item in files if ".uploading" not in item.parts and ".source." not in item.name), None)
        path = path or next((item for item in files if ".uploading" in item.parts), None)
        if path is None:
            raise ImageNormalizationError("ATTACHMENT_NOT_FOUND", "附件不存在")
        return path, descriptor.content_type

    def resolve_for_model(self, task_id: int, image_path: str, model_name: str) -> NormalizedImage:
        """Validate a Run image path and normalize the digest it names."""

        dirs = self._dirs(task_id)
        directory = dirs.attachment_dir
        candidate = self._inside(directory, dirs.workspace_root / image_path)
        if candidate.parent != directory or not candidate.is_file():
            raise ImageNormalizationError("ATTACHMENT_NOT_FOUND", "运行引用的图片不存在")
        asset_id = _asset_id_from_name(candidate.name)
        if asset_id is None:
            raise ImageNormalizationError("ATTACHMENT_NOT_FOUND", "运行引用的图片不存在")
        return self.finalize(task_id, asset_id, model_name)

    def relative_path(self, task_id: int, path: Path) -> str:
        """Convert a checked attachment path to a stable workspace-relative path."""

        dirs = self._dirs(task_id)
        checked = self._inside(dirs.attachment_dir, path)
        return checked.relative_to(dirs.workspace_root).as_posix()

    def delete(self, task_id: int, asset_id: str) -> None:
        """Delete a digest asset only when no task in its workspace references it."""

        directory = self._dirs(task_id).attachment_dir
        asset_id = self._safe_asset_id(asset_id)
        task = self._tasks.get_task(task_id)
        for sibling in self._tasks.list_tasks_for_workspace(task.workspace_id):
            for run in self._runs.list_runs_for_task(sibling.id):
                if any(_asset_id_from_name(Path(str(path)).name) == asset_id for path in (run.image_paths or [])):
                    raise ImageNormalizationError("ATTACHMENT_NOT_OWNED", "已发送消息仍在使用该附件")
        for path in self._files(directory, asset_id):
            path.unlink(missing_ok=True)


__all__ = ["AttachmentDescriptor", "AttachmentService", "collect_workspace_orphans"]
