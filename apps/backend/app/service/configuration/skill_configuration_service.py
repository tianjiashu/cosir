"""系统级 Agent Skill 的发现、启停、导入和删除用例。"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO

from app.config.logging.logger import log
from app.service.configuration.file_store import ConfigurationFileStore
from app.service.skills.skill_catalog import SkillCatalog, SkillInfo, read_skill_metadata
from app.service.skills.skill_configuration import SkillConfiguration, read_skill_configuration
from app.utils.path.system_cosir import (
    system_cosir_dir,
    system_skills_config_file,
    system_skills_dir,
)

MAX_SKILL_IMPORT_FILES = 512
MAX_SKILL_IMPORT_ENTRIES = 1024
MAX_SKILL_IMPORT_FILE_BYTES = 16 * 1024 * 1024
MAX_SKILL_IMPORT_TOTAL_BYTES = 128 * 1024 * 1024
MAX_SKILL_ARCHIVE_BYTES = 128 * 1024 * 1024
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


@dataclass(frozen=True)
class SkillDocument:
    """供系统配置界面展示的 Skill 元数据与正文。"""

    info: SkillInfo
    content: str


class SkillConfigurationService:
    """管理系统目录内的 Skill 文件和启用名单。"""

    def __init__(self) -> None:
        self.root = system_skills_dir()
        self.configuration_path = system_skills_config_file()
        self.cosir_root = system_cosir_dir()
        self.store = ConfigurationFileStore()

    def list(self) -> list[SkillInfo]:
        """列出可用 Skill 及其当前启用状态。"""

        return SkillCatalog().list_skills()

    def read(self, name: str) -> SkillDocument:
        """读取指定 Skill 的说明正文；配置界面查看不受启用状态限制。"""

        catalog = SkillCatalog()
        info, content = catalog.read_document(name)
        return SkillDocument(info=info, content=content)

    def update_enabled(self, enabled_names: list[str]) -> list[SkillInfo]:
        """保存启用名单，并返回更新后的 Skill 清单。"""

        available = {skill.name for skill in self.list()}
        unknown = set(enabled_names) - available
        if unknown:
            raise ValueError(f"未找到 Skill：{', '.join(sorted(unknown))}")
        self._write_configuration(SkillConfiguration(enabled_skills=enabled_names))
        log.info(
            "configuration_skills_enabled_written",
            extra={
                "msg": "Skill 启用状态已保存",
                "data": {"enabled_count": len(set(enabled_names))},
            },
        )
        return self.list()

    def import_directory(self, source_path: str) -> SkillInfo:
        """从原生选择器选中的目录复制并安装；拒绝目录内部链接，根目录链接按用户选择解析。"""

        source = self._resolve_selected_path(source_path)
        if not source.is_dir():
            raise ValueError("请选择 Skill 文件夹")

        self.root.mkdir(parents=True, exist_ok=True)
        total_bytes = 0
        file_count = 0
        entry_count = 0
        with tempfile.TemporaryDirectory(prefix="skill-import-", dir=self.root) as temporary:
            staging = Path(temporary) / source.name
            staging.mkdir()
            for current, directory_names, file_names in os.walk(
                source,
                followlinks=False,
                onerror=self._raise_directory_walk_error,
            ):
                directory_names.sort()
                file_names.sort()
                current_path = Path(current)
                entry_count += len(directory_names) + len(file_names)
                if entry_count > MAX_SKILL_IMPORT_ENTRIES:
                    raise ValueError("Skill 文件夹条目超过 1024 个")
                for directory_name in directory_names:
                    if (current_path / directory_name).is_symlink():
                        raise ValueError("Skill 文件夹不能包含符号链接")
                for file_name in file_names:
                    source_file = current_path / file_name
                    relative_path = source_file.relative_to(source)
                    import_path = PurePosixPath(source.name, *relative_path.parts)
                    if self._is_ignored_import_path(import_path):
                        continue
                    if source_file.is_symlink():
                        raise ValueError("Skill 文件夹不能包含符号链接")
                    file_count += 1
                    file_size = source_file.stat().st_size
                    checked_total_bytes = self._check_import_size(file_count, file_size, total_bytes)
                    target = staging.joinpath(*relative_path.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    copied_bytes = self._copy_import_file(source_file, target, total_bytes)
                    if copied_bytes != file_size:
                        raise ValueError("Skill 文件大小在导入过程中发生变化，请重新选择")
                    total_bytes = checked_total_bytes
            if file_count == 0:
                raise ValueError("所选文件夹中没有可导入的 Skill 文件")
            return self._install_staged_skill(staging)

    def import_archive(self, source_path: str) -> SkillInfo:
        """从原生文件选择器选中的 ZIP 中限量解压并安装 Skill。"""

        source = self._resolve_selected_path(source_path)
        if not source.is_file():
            raise ValueError("请选择 Skill ZIP 文件")
        if source.suffix.lower() != ".zip":
            raise ValueError("请选择 .zip 格式的 Skill 文件")
        if source.stat().st_size > MAX_SKILL_ARCHIVE_BYTES:
            raise ValueError("Skill ZIP 文件超过 128 MiB 上限")

        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(source) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_SKILL_IMPORT_ENTRIES:
                    raise ValueError("Skill ZIP 文件条目超过 1024 个")
                files = [entry for entry in entries if not entry.is_dir()]
                relative_paths = [self._relative_import_path(entry.filename) for entry in files]
                import_paths = [
                    path for path in relative_paths if not self._is_ignored_import_path(path)
                ]
                if not import_paths:
                    raise ValueError("Skill ZIP 中没有可导入的文件")
                if len(import_paths) > MAX_SKILL_IMPORT_FILES:
                    raise ValueError("Skill 文件数量超过 512 个")
                if len({path.as_posix().casefold() for path in import_paths}) != len(import_paths):
                    raise ValueError("Skill ZIP 中存在重复文件路径")
                root_names = {path.parts[0] for path in import_paths}
                if len(root_names) != 1 or any(len(path.parts) < 2 for path in import_paths):
                    raise ValueError("ZIP 中必须包含单个 Skill 文件夹")

                selected_name = next(iter(root_names))
                with tempfile.TemporaryDirectory(prefix="skill-import-", dir=self.root) as temporary:
                    staging = Path(temporary) / selected_name
                    staging.mkdir()
                    total_bytes = 0
                    file_count = 0
                    for entry, relative_path in zip(files, relative_paths, strict=True):
                        if self._is_ignored_import_path(relative_path):
                            continue
                        if stat.S_ISLNK(entry.external_attr >> 16):
                            raise ValueError("Skill ZIP 不能包含符号链接")
                        file_count += 1
                        previous_total_bytes = total_bytes
                        total_bytes = self._check_import_size(file_count, entry.file_size, total_bytes)
                        target = staging.joinpath(*relative_path.parts[1:])
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(entry) as source_file:
                            copied = self._copy_import_stream(source_file, target, previous_total_bytes)
                        if copied != entry.file_size:
                            raise ValueError("Skill ZIP 文件大小在导入过程中发生变化")
                    return self._install_staged_skill(staging)
        except zipfile.BadZipFile as exc:
            raise ValueError("无法读取 Skill ZIP 文件") from exc

    def _install_staged_skill(self, staging: Path) -> SkillInfo:
        """校验暂存目录并原子安装，配置写入失败时回滚目录。"""

        info = read_skill_metadata(staging)
        destination = self.root / info.name
        if destination.exists():
            raise FileExistsError(f"已存在同名 Skill：{info.name}")
        staging.rename(destination)

        try:
            configuration = read_skill_configuration()
            if configuration.enabled_skills is not None:
                enabled_names = list(configuration.enabled_skills)
                if info.name not in enabled_names:
                    enabled_names.append(info.name)
                self._write_configuration(SkillConfiguration(enabled_skills=enabled_names))
        except Exception:
            try:
                shutil.rmtree(destination)
            except OSError:
                log.exception(
                    "configuration_skill_import_rollback_failed",
                    extra={
                        "msg": "Skill 导入配置读取或写入失败，且未能清理已复制目录",
                        "data": {"skill_name": info.name},
                    },
                )
            raise

        imported = SkillInfo(info.name, info.description, destination, True)
        log.info(
            "configuration_skill_imported",
            extra={"msg": "Skill 已导入系统目录", "data": {"skill_name": info.name}},
        )
        return imported

    @staticmethod
    def _is_ignored_import_path(path: PurePosixPath) -> bool:
        return "__MACOSX" in path.parts or path.name == ".DS_Store" or path.name.startswith("._")

    @staticmethod
    def _resolve_selected_path(source_path: str) -> Path:
        source = Path(source_path)
        if not source.is_absolute():
            raise ValueError("Skill 导入路径必须是本机绝对路径")
        try:
            return source.resolve(strict=True)
        except FileNotFoundError as error:
            raise ValueError("所选 Skill 路径已不可用，请重新选择") from error

    @staticmethod
    def _raise_directory_walk_error(error: OSError) -> None:
        raise error

    @staticmethod
    def _check_import_size(file_count: int, file_size: int, total_bytes: int) -> int:
        if file_count > MAX_SKILL_IMPORT_FILES:
            raise ValueError("Skill 文件数量超过 512 个")
        if file_size > MAX_SKILL_IMPORT_FILE_BYTES:
            raise ValueError("Skill 单个文件超过 16 MiB 上限")
        total_bytes += file_size
        if total_bytes > MAX_SKILL_IMPORT_TOTAL_BYTES:
            raise ValueError("Skill 文件总大小超过 128 MiB 上限")
        return total_bytes

    @classmethod
    def _copy_import_file(cls, source: Path, target: Path, previous_total_bytes: int) -> int:
        with source.open("rb") as source_file:
            return cls._copy_import_stream(source_file, target, previous_total_bytes)

    @staticmethod
    def _copy_import_stream(source: BinaryIO, target: Path, previous_total_bytes: int) -> int:
        """分块写入暂存目录，按实际读取字节数阻止超限内容落盘。"""

        copied_bytes = 0
        with target.open("wb") as target_file:
            while chunk := source.read(1024 * 1024):
                copied_bytes += len(chunk)
                if copied_bytes > MAX_SKILL_IMPORT_FILE_BYTES:
                    raise ValueError("Skill 单个文件超过 16 MiB 上限")
                if previous_total_bytes + copied_bytes > MAX_SKILL_IMPORT_TOTAL_BYTES:
                    raise ValueError("Skill 文件总大小超过 128 MiB 上限")
                target_file.write(chunk)
        return copied_bytes

    def delete(self, name: str) -> None:
        """删除已发现 Skill，并从显式启用名单中移除其名称。"""

        info = next((skill for skill in self.list() if skill.name == name), None)
        if info is None:
            raise KeyError(name)
        with tempfile.TemporaryDirectory(prefix="skill-delete-", dir=self.root) as temporary:
            staged_directory = Path(temporary) / name
            info.directory.rename(staged_directory)
            try:
                configuration = read_skill_configuration()
                if configuration.enabled_skills is not None:
                    enabled_names = [item for item in configuration.enabled_skills if item != name]
                    self._write_configuration(SkillConfiguration(enabled_skills=enabled_names))
            except Exception:
                staged_directory.rename(info.directory)
                raise
        log.info(
            "configuration_skill_deleted",
            extra={"msg": "Skill 已从系统目录删除", "data": {"skill_name": name}},
        )

    def _write_configuration(self, configuration: SkillConfiguration) -> None:
        serialized = (
            json.dumps(configuration.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
        )
        self.store.write_text_atomic(
            self.configuration_path,
            serialized,
            root=self.cosir_root,
        )

    @staticmethod
    def _relative_import_path(value: str) -> PurePosixPath:
        """只接受 ZIP 内位于单个 Skill 根目录下的相对路径。"""

        path = PurePosixPath(value)
        windows_path = PureWindowsPath(value)
        if (
            path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or not path.parts
            or ".." in path.parts
            or "\\" in value
            or any(
                part.endswith((" ", "."))
                or any(ord(character) < 32 or character in '<>:"|?*' for character in part)
                or part.split(".", maxsplit=1)[0].upper() in WINDOWS_RESERVED_NAMES
                for part in path.parts
            )
        ):
            raise ValueError("导入文件路径必须位于所选 Skill 文件夹内")
        return path
