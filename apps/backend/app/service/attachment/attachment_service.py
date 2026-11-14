"""Workspace ``.cosir/Attachment`` 图片存储。

规范化后的文件字节是附件事实源；其小写 SHA-256 摘要同时作为资源标识和文件名主体，
不使用附件数据库表。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from app.config.constant import Constant
from app.config.logging.logger import log
from app.service import depends as service_depends
from app.service.attachment.image_normalizer import (
    ImageNormalizationError,
    StoredImage,
    normalize_image,
)
from app.utils.path.workspace_cosir import (
    workspace_attachment_dir,
    workspace_attachment_staging_dir,
    workspace_cosir_dir,
)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = os.lstat(path).st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        return path.is_symlink()
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _normal_path(path: Path) -> str:
    """返回用于比较目录真实位置的规范化绝对路径。"""

    return os.path.normcase(os.path.abspath(path))


def _assert_real_directory(path: Path, *, create: bool) -> Path:
    """确保附件目录是普通目录，并拒绝符号链接或 reparse point。"""

    if path.exists() or _is_reparse_point(path):
        if _is_reparse_point(path) or _normal_path(path.resolve(strict=False)) != _normal_path(
            path
        ):
            raise ImageNormalizationError("ATTACHMENT_STORAGE_UNAVAILABLE", "附件存储目录不可用")
        if not path.is_dir():
            raise ImageNormalizationError("ATTACHMENT_STORAGE_UNAVAILABLE", "附件存储目录不可用")
        return path
    if not create:
        return path
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ImageNormalizationError(
            "ATTACHMENT_STORAGE_UNAVAILABLE", "附件存储目录不可用"
        ) from exc
    if _is_reparse_point(path) or _normal_path(path.resolve(strict=False)) != _normal_path(path):
        raise ImageNormalizationError("ATTACHMENT_STORAGE_UNAVAILABLE", "附件存储目录不可用")
    return path


def _asset_id_from_name(name: str) -> str | None:
    match = Constant.Attachment.ASSET_FILE.fullmatch(name)
    return match.group("asset_id") if match else None


@dataclass(frozen=True, slots=True)
class AttachmentDescriptor:
    """图片上传和内容接口返回的附件元数据。"""

    asset_id: str
    name: str
    content_type: str
    byte_size: int
    width: int
    height: int
    locator: str
    status: str


@dataclass(frozen=True, slots=True)
class SavedImage:
    """工具产出图片的发布结果：附件描述，以及模型请求里引用它用的 workspace 相对路径。"""

    descriptor: AttachmentDescriptor
    path: str


@dataclass(frozen=True, slots=True)
class _WorkspaceAttachmentDirs:
    """一个 workspace 的根目录与附件落盘目录（仅本模块内部使用）。"""

    workspace_root: Path
    attachment_dir: Path


class AttachmentService:
    """管理永久保留的 workspace 级图片资源。

    上传、读取和归一化只以 ``workspace_id`` 为资源边界：图片按 SHA-256
    幂等保存到该 workspace 的 ``.cosir/Attachment`` 目录，不创建附件数据库
    记录，也不依赖 Session 或 Run 才能完成上传。附件一旦成功发布便永久保留，
    不因 Composer、Run、Session 或 workspace 的引用变化而删除。
    """

    def __init__(self) -> None:
        self._workspaces = service_depends.get_workspace_service()

    def _dirs(self, workspace_id: int) -> _WorkspaceAttachmentDirs:
        """返回 workspace 根目录与附件落盘目录。

        ``workspace_root`` 直接取自 workspace 记录，**不**由附件目录反推——避免把 ``.cosir``
        的目录层级知识散落到本模块（层级规则由 ``app.utils.path`` 独占）。

        参数:
            workspace_id: 工作区标识。

        返回:
            ``_WorkspaceAttachmentDirs``：``workspace_root`` 为解析后的 workspace 根，
            ``attachment_dir`` 为 ``<root>/.cosir/Attachment``。

        异常:
            KeyError: workspace 不存在（由依赖 service 抛出）。
            ImageNormalizationError: 附件目录不可用（reparse point / 创建失败）。

        副作用:
            可能按需创建 ``<root>/.cosir``、``<root>/.cosir/Attachment`` 与
            ``<root>/.cosir/Attachment/.uploading``。
        """

        workspace = self._workspaces.get_workspace(workspace_id)
        root = Path(workspace.root_path).resolve()
        cosir = workspace_cosir_dir(root)
        directory = workspace_attachment_dir(root)
        staging = workspace_attachment_staging_dir(root)
        _assert_real_directory(cosir, create=True)
        _assert_real_directory(directory, create=True)
        _assert_real_directory(staging, create=True)

        return _WorkspaceAttachmentDirs(
            workspace_root=Path(workspace.root_path).resolve(),
            attachment_dir=directory,
        )

    @staticmethod
    def _safe_asset_id(asset_id: str) -> str:
        if not Constant.Attachment.ASSET_ID.fullmatch(asset_id):
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
                raise ImageNormalizationError(
                    "ATTACHMENT_STORAGE_UNAVAILABLE", "附件存储目录不可用"
                )
            if path.is_file() and _asset_id_from_name(path.name) == asset_id:
                files.append(self._inside(directory, path))
        return files

    async def upload(self, workspace_id: int, file: UploadFile) -> AttachmentDescriptor:
        """校验、规范化并幂等保存一张 workspace 级 JPEG/PNG 图片。

        上传不要求 Session 或 Run 已存在，也不建立任何消息引用。原始流先写入 workspace 临时目录，
        再交给与 :meth:`save_image` 共用的发布内核（:meth:`_publish`）。

        参数:
            workspace_id: 附件归属的工作区标识。
            file: FastAPI 上传文件流。

        返回:
            ``AttachmentDescriptor``，包含稳定的 asset id 和
            ``cosir-attachment://`` locator。

        异常:
            ``KeyError``: workspace 不存在。
            ``ImageNormalizationError``: 文件过大、内容不是有效图片、格式不支持、
                规范化后文件过大或 workspace 附件目录不可用。

        副作用:
            在 workspace 的 ``.cosir/Attachment`` 下写入或复用图片文件；不写入
            SQLite，不绑定 Session/Run。已发布文件不会由附件服务回收。
        """

        workspace_dirs = self._dirs(workspace_id)
        source_tmp = (
            workspace_attachment_staging_dir(workspace_dirs.workspace_root)
            / f".{uuid4().hex}.raw.part"
        )
        source_created = False
        total = 0
        try:
            with source_tmp.open("xb") as output:
                source_created = True
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > Constant.Attachment.MAX_UPLOAD_BYTES:
                        raise ImageNormalizationError("ATTACHMENT_FILE_TOO_LARGE", "附件文件过大")
                    output.write(chunk)
            descriptor, _ = self._publish(
                workspace_id, workspace_dirs, source_tmp, file.filename, total
            )
            return descriptor
        except OSError as exc:
            raise ImageNormalizationError(
                "ATTACHMENT_STORAGE_UNAVAILABLE", "附件存储目录不可用"
            ) from exc
        finally:
            if source_created:
                source_tmp.unlink(missing_ok=True)
            await file.close()

    def save_image(self, workspace_id: int, data: bytes, name: str) -> SavedImage:
        """工具在内存里产出的图片（如浏览器截图）按与上传完全相同的规则规范化并幂等发布。

        本机已有的图片文件走 :meth:`import_image`。与 :meth:`upload` 共用发布内核：同一套 EXIF 方向、最长边与大小策略，同一个附件目录。
        返回值另带 workspace 相对路径——模型请求里的图片定位符用它（见 ``vision_input``），
        不用 asset id。同步阻塞 IO，只在工具线程调用。

        异常:
            ``KeyError``: workspace 不存在。
            ``ImageNormalizationError``: 图片过大、不是有效图片或附件目录不可用。
        """

        if len(data) > Constant.Attachment.MAX_UPLOAD_BYTES:
            raise ImageNormalizationError("ATTACHMENT_FILE_TOO_LARGE", "附件文件过大")
        workspace_dirs = self._dirs(workspace_id)
        source_tmp = (
            workspace_attachment_staging_dir(workspace_dirs.workspace_root)
            / f".{uuid4().hex}.raw.part"
        )
        try:
            source_tmp.write_bytes(data)
            descriptor, target = self._publish(
                workspace_id, workspace_dirs, source_tmp, name, len(data)
            )
            return SavedImage(descriptor=descriptor, path=self.relative_path(workspace_id, target))
        except OSError as exc:
            raise ImageNormalizationError(
                "ATTACHMENT_STORAGE_UNAVAILABLE", "附件存储目录不可用"
            ) from exc
        finally:
            source_tmp.unlink(missing_ok=True)

    def import_image(self, workspace_id: int, source: Path) -> SavedImage:
        """本机已有的图片文件（工作区内外均可，如 ``read_image`` 读到的路径）按上传规则规范化并幂等发布。

        源文件只读、直接作为规范化输入，不先复制进暂存区；规范化产物仍写在本工作区的暂存目录里。
        同一份内容重复导入得到同一个 asset（SHA-256 去重），导入已发布的附件本身也是幂等的。
        同步阻塞 IO，只在工具线程调用。

        异常:
            ``KeyError``: workspace 不存在。
            ``ImageNormalizationError``: 文件不存在或不可读（``ATTACHMENT_NOT_FOUND``）、不是普通文件
                （``ATTACHMENT_NOT_A_FILE``）、过大、不是有效图片、格式不支持或附件目录不可用。
        """

        try:
            source_stat = source.stat()
        except OSError as exc:
            raise ImageNormalizationError("ATTACHMENT_NOT_FOUND", "图片文件不存在或不可读") from exc
        if not stat.S_ISREG(source_stat.st_mode):
            raise ImageNormalizationError("ATTACHMENT_NOT_A_FILE", "路径不是普通文件")
        if source_stat.st_size > Constant.Attachment.MAX_UPLOAD_BYTES:
            raise ImageNormalizationError("ATTACHMENT_FILE_TOO_LARGE", "附件文件过大")
        workspace_dirs = self._dirs(workspace_id)
        try:
            descriptor, target = self._publish(
                workspace_id, workspace_dirs, source, source.name, source_stat.st_size
            )
        except OSError as exc:
            raise ImageNormalizationError(
                "ATTACHMENT_STORAGE_UNAVAILABLE", "附件存储目录不可用"
            ) from exc
        return SavedImage(descriptor=descriptor, path=self.relative_path(workspace_id, target))

    def _publish(
        self,
        workspace_id: int,
        workspace_dirs: _WorkspaceAttachmentDirs,
        source: Path,
        name: str | None,
        source_bytes: int,
    ) -> tuple[AttachmentDescriptor, Path]:
        """发布内核：规范化源文件，以规范化后字节的 SHA-256 为资源标识原子发布到附件目录。

        上传、工具内存图片与本机图片文件三个入口共用这里，图片规则只有这一份。``source`` 只读；
        调用方负责清理自己建的暂存源文件，规范化产物由这里清理。返回描述与正式文件路径。
        """

        directory = workspace_dirs.attachment_dir
        normalized_tmp = (
            workspace_attachment_staging_dir(workspace_dirs.workspace_root)
            / f".{uuid4().hex}.normalized.part"
        )
        try:
            normalized = normalize_image(source, normalized_tmp)
            if normalized.byte_size > Constant.Attachment.MAX_IMAGE_BYTES:
                raise ImageNormalizationError("ATTACHMENT_FILE_TOO_LARGE", "规范化后的图片过大")
            digest = hashlib.sha256()
            with normalized.path.open("rb") as normalized_file:
                for chunk in iter(lambda: normalized_file.read(1024 * 1024), b""):
                    digest.update(chunk)
            asset_id = digest.hexdigest()
            target = directory / f"{asset_id}.{normalized.target_format}"
            # Hard-link 是不覆盖发布：并发发布相同规范化内容时只保留一个正式文件。
            with contextlib.suppress(FileExistsError):
                os.link(normalized.path, target)
        finally:
            normalized_tmp.unlink(missing_ok=True)
        files = self._files(directory, asset_id)
        if not files or target not in files:
            raise ImageNormalizationError("ATTACHMENT_STORAGE_UNAVAILABLE", "附件存储目录不可用")
        descriptor = AttachmentDescriptor(
            asset_id=asset_id,
            name=name or f"{asset_id}.{normalized.target_format}",
            content_type=normalized.content_type,
            byte_size=normalized.byte_size,
            width=normalized.width,
            height=normalized.height,
            locator=f"cosir-attachment://{asset_id}",
            status="ready",
        )
        log.info(
            "attachment_uploaded",
            extra={
                "msg": "图片附件已规范化并按 SHA-256 保存",
                "data": {
                    "workspace_id": workspace_id,
                    "asset_id": asset_id,
                    "source_bytes": source_bytes,
                    "stored_bytes": normalized.byte_size,
                    "width": normalized.width,
                    "height": normalized.height,
                },
            },
        )
        return descriptor, target

    def resolve_content(self, workspace_id: int, asset_id: str) -> tuple[Path, str]:
        """返回已发布图片文件及其 MIME 类型，不重新解析图片内容。"""

        stored = self._stored_image(workspace_id, asset_id)
        return stored.path, stored.content_type

    def _stored_image(self, workspace_id: int, asset_id: str) -> StoredImage:
        """解析一个已发布的 workspace 图片，不触发 Pillow 解析或格式转换。"""

        safe_asset_id = self._safe_asset_id(asset_id)
        directory = self._dirs(workspace_id).attachment_dir
        path = next(
            (
                item
                for item in self._files(directory, safe_asset_id)
                if ".uploading" not in item.parts
            ),
            None,
        )
        if path is None:
            raise ImageNormalizationError("ATTACHMENT_NOT_FOUND", "附件不存在")
        match = Constant.Attachment.ASSET_FILE.fullmatch(path.name)
        if match is None:
            raise ImageNormalizationError("ATTACHMENT_NOT_FOUND", "附件不存在")
        content_type = Constant.Attachment.CONTENT_TYPES[match.group("extension")]
        return StoredImage(path=path, content_type=content_type, byte_size=path.stat().st_size)

    def resolve_image_assets_for_run(self, workspace_id: int, asset_ids: list[str]) -> list[str]:
        """校验本次 Run 的图片聚合限制并返回已发布图片的 workspace 相对路径。

        单张图片的格式、内容、尺寸和单文件大小在上传阶段已经完成；本方法只处理一次
        Run 的图片数量与总字节限制，并确认资源仍属于当前 workspace。它不读取图片内容，
        也不执行规范化。

        异常:
            ImageNormalizationError: 图片数量或总大小超限、资源不存在、格式不符合策略，
                或附件存储目录不可用。
        """

        if len(asset_ids) > Constant.Attachment.MAX_IMAGES_PER_REQUEST:
            raise ImageNormalizationError("ATTACHMENT_TOO_MANY_IMAGES", "图片数量超过统一输入限制")
        total_bytes = 0
        paths: list[str] = []
        for asset_id in asset_ids:
            stored = self._stored_image(workspace_id, asset_id)
            total_bytes += stored.byte_size
            paths.append(self.relative_path(workspace_id, stored.path))
        if total_bytes > Constant.Attachment.REQUEST_TOTAL_MAX_BYTES:
            raise ImageNormalizationError(
                "ATTACHMENT_REQUEST_TOO_LARGE", "图片总大小超过统一输入限制"
            )
        return paths

    def resolve_for_model(self, workspace_id: int, image_path: str) -> StoredImage:
        """校验 Run 图片路径并返回已规范化的附件，不重复执行图片策略校验。"""

        dirs = self._dirs(workspace_id)
        directory = dirs.attachment_dir
        try:
            candidate = self._inside(directory, dirs.workspace_root / image_path)
        except ImageNormalizationError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise ImageNormalizationError("ATTACHMENT_NOT_FOUND", "运行引用的图片不存在") from exc
        if candidate.parent != directory or not candidate.is_file():
            raise ImageNormalizationError("ATTACHMENT_NOT_FOUND", "运行引用的图片不存在")
        asset_id = _asset_id_from_name(candidate.name)
        if asset_id is None:
            raise ImageNormalizationError("ATTACHMENT_NOT_FOUND", "运行引用的图片不存在")
        return self._stored_image(workspace_id, asset_id)

    def relative_path(self, workspace_id: int, path: Path) -> str:
        """将已完成安全检查的附件路径转换为稳定的 workspace 相对路径。"""

        dirs = self._dirs(workspace_id)
        checked = self._inside(dirs.attachment_dir, path)
        return checked.relative_to(dirs.workspace_root).as_posix()


__all__ = ["AttachmentDescriptor", "AttachmentService", "SavedImage"]
