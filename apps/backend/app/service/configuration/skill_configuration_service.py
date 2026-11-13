"""一个作用域（系统级或 workspace 级）内 Agent Skill 的发现、启停、导入和删除用例。

写操作只作用于本作用域自己的目录与启用名单：workspace 配置页能看到继承来的系统级 Skill，
但不能启停、覆盖写入或删除它们。
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO

from app.config.logging.logger import log
from app.service.configuration.extension_scope import ExtensionScope
from app.service.configuration.file_store import ConfigurationFileStore
from app.service.configuration.runtime_notices.catalog_change import (
    CatalogChange,
    broadcast_catalog_changes,
)
from app.service.configuration.skill_catalog import (
    SkillCatalog,
    SkillDocument,
    SkillInfo,
    read_skill_metadata,
)
from app.service.configuration.skill_configuration import (
    SkillConfiguration,
    read_skill_configuration,
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


class SkillConfigurationService:
    """管理一个作用域的 Skill 文件和启用名单；``workspace_root`` 为空即系统级。"""

    def __init__(self, *, workspace_root: Path | None = None) -> None:
        self.scope = ExtensionScope(workspace_root)
        self.store = ConfigurationFileStore()

    @property
    def catalog(self) -> SkillCatalog:
        """本作用域配置页看得到的目录：系统级只有系统层，workspace 级是两层合并。"""

        return SkillCatalog.visible_from(self.scope.workspace_root)

    def list(self) -> list[SkillInfo]:
        """列出可见 Skill 及其启用状态、所属作用域与覆盖关系。"""

        return self.catalog.list_skills()

    def read(self, name: str) -> SkillDocument:
        """读取指定 Skill 的说明正文；配置界面查看不受启用状态限制。"""

        return self.catalog.read_document(name)

    def set_enabled(self, name: str, enabled: bool) -> SkillInfo:
        """启用或停用本作用域的一个 Skill，返回更新后的条目。

        异常:
            KeyError: 没有这个 Skill。
            ValueError: 它属于另一个作用域（例如在 workspace 页启停系统级 Skill）。
        """

        with self.store.locked(self.scope.skills_config_file):
            self._own(name)
            before = self._agent_view()
            configuration = read_skill_configuration(self.scope)
            if configuration.enabled_skills is None:
                # 名单文件不存在时等于「本作用域全部启用」，第一次启停时把这个隐含名单落成显式名单。
                names = [
                    skill.name
                    for skill in self.catalog.list_skills()
                    if skill.scope.key == self.scope.key
                ]
            else:
                names = list(configuration.enabled_skills)
            if enabled and name not in names:
                names.append(name)
            if not enabled:
                names = [item for item in names if item != name]
            self._write_configuration(SkillConfiguration(enabled_skills=names))
        self._publish(before)
        log.info(
            "configuration_skill_enabled_written",
            extra={
                "msg": "Skill 启用状态已保存",
                "data": {"scope": self.scope.key, "skill_name": name, "enabled": enabled},
            },
        )
        return self.catalog.find(name)

    def _agent_view(self) -> dict[str, str | None]:
        """Agent 在本作用域看到的 Skill 目录（启用项的名称 → 描述），用来比较变更前后。"""

        return {skill.name: skill.description for skill in self.catalog.list_skills() if skill.enabled}

    def _publish(self, before: dict[str, str | None]) -> None:
        """写入已提交后，通知固化了 ``skill`` 工具的 Session 目录有哪些可见变化。"""

        broadcast_catalog_changes(
            CatalogChange.between(
                "skills",
                scope=None if self.scope.workspace_root is None else self.scope.key,
                before=before,
                after=self._agent_view(),
                shadowed_in=SkillCatalog.workspace_defines if self.scope.workspace_root is None else None,
            )
        )

    def _own(self, name: str) -> SkillInfo:
        """取本作用域自己的 Skill；继承来的条目只读。"""

        info = self.catalog.find(name)
        if info.scope.key != self.scope.key:
            raise ValueError("这个 Skill 继承自系统级，请在系统设置中修改")
        return info

    def import_directory(self, source_path: str) -> SkillInfo:
        """从原生选择器选中的目录复制并安装；拒绝目录内部链接，根目录链接按用户选择解析。"""

        source = self._resolve_selected_path(source_path)
        if not source.is_dir():
            raise ValueError("请选择 Skill 文件夹")

        total_bytes = 0
        file_count = 0
        entry_count = 0
        with self._staging_directory("skill-import-") as temporary:
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
                with self._staging_directory("skill-import-") as temporary:
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
        """校验暂存目录并原子安装到本作用域，配置写入失败时回滚目录。

        只拒绝本作用域内的同名目录：workspace 导入与系统级同名的 Skill 是有意的覆盖。
        """

        with self.store.locked(self.scope.skills_config_file):
            before = self._agent_view()
            configuration = read_skill_configuration(self.scope)
            info = read_skill_metadata(staging, self.scope, configuration)
            destination = self.scope.skills_dir / info.name
            if destination.exists():
                raise FileExistsError(f"已存在同名 Skill：{info.name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(destination)

            try:
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
                            "msg": "Skill 导入配置写入失败，且未能清理已复制目录",
                            "data": {"scope": self.scope.key, "skill_name": info.name},
                        },
                    )
                raise

        self._publish(before)
        log.info(
            "configuration_skill_imported",
            extra={
                "msg": "Skill 已导入",
                "data": {"scope": self.scope.key, "skill_name": info.name},
            },
        )
        return self.catalog.find(info.name)

    def _staging_directory(self, prefix: str) -> tempfile.TemporaryDirectory[str]:
        """导入 / 删除用的暂存目录：放在 ``.cosir`` 根下而不是 skills 目录里。

        放在 skills 目录里会在导入期间被目录扫描当成「元数据无效的 Skill」告警；放在同一个
        ``.cosir`` 下保证与 skills 目录同盘，``rename`` 仍是原子的。
        """

        self.scope.cosir_dir.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(prefix=f".{prefix}", dir=self.scope.cosir_dir)

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
        """删除本作用域的一个 Skill，并从显式启用名单中移除其名称。

        异常:
            KeyError: 没有这个 Skill。
            ValueError: 它继承自系统级。
        """

        with self.store.locked(self.scope.skills_config_file):
            info = self._own(name)
            before = self._agent_view()
            with self._staging_directory("skill-delete-") as temporary:
                staged_directory = Path(temporary) / name
                info.directory.rename(staged_directory)
                try:
                    configuration = read_skill_configuration(self.scope)
                    if configuration.enabled_skills is not None:
                        enabled_names = [item for item in configuration.enabled_skills if item != name]
                        self._write_configuration(SkillConfiguration(enabled_skills=enabled_names))
                except Exception:
                    staged_directory.rename(info.directory)
                    raise
        self._publish(before)
        log.info(
            "configuration_skill_deleted",
            extra={"msg": "Skill 已删除", "data": {"scope": self.scope.key, "skill_name": name}},
        )

    def _write_configuration(self, configuration: SkillConfiguration) -> None:
        serialized = (
            json.dumps(configuration.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
        )
        self.store.write_text_atomic(
            self.scope.skills_config_file,
            serialized,
            root=self.scope.cosir_dir,
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
