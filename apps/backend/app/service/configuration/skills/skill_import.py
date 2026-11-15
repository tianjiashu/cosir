"""把本机 Skill 文件夹或 ZIP 导入一个作用域的 cosir 专属目录（``.cosir/skills``）。

只写专属目录：在 cosir 里导入就是给 cosir 用，不应出现在读共享目录 ``.agents/skills`` 的 Codex 等工具里。
来源是用户经原生选择器选中的本机路径，属于进程外输入：先限量复制或解压进暂存点目录，按严格标准校验，
再原子移到位。

装之前先判定它和本页看得到的同名 Skill 的关系（按指纹与根目录优先级，事实全部来自 ``SkillCatalog``）：

- ``identical``：同名的生效条目与它内容相同，导入对 Agent 没有任何变化，不接受；
- ``replace``：本作用域专属目录里已有同名、内容不同的（不论目录名），用户选「替换」才装，旧目录删除；
- ``override``：别处（共享目录、系统级）有同名、内容不同的，用户选「仍然导入」才装，别处那份原样保留。

判定与安装在同一把锁里完成，用户确认后的第二次请求重新判定一次：两次之间关系变了就再次抛出冲突，
不信任前端记住的旧结论。
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO, ClassVar, Literal

from app.config.logging.logger import log
from app.service.configuration import file_store
from app.service.configuration.extension_scope import ExtensionScope
from app.service.configuration.skills.catalog import (
    SkillCatalog,
    SkillInfo,
    read_skill_metadata,
)
from app.service.configuration.skills.configuration import SkillConfiguration
from app.service.configuration.skills.skill_root import SkillRoot

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


SkillImportResolution = Literal["replace", "override"]
SkillImportRelation = Literal["identical", "replace", "override"]


class SkillImportConflict(Exception):
    """导入与现有同名 Skill 冲突，要用户决定；接入层映射为 409 并带上明细。

    ``existing`` 是本页看得到的全部同名条目，按优先级从高到低。``identical`` 没有可选的处理方式。
    """

    _MESSAGES: ClassVar[dict[SkillImportRelation, str]] = {
        "identical": "已经有这个 Skill 了，内容相同",
        "replace": "cosir 专属目录里已有同名 Skill，内容不同",
        "override": "别处已有同名 Skill，内容不同",
    }

    def __init__(
        self, relation: SkillImportRelation, incoming: SkillInfo, existing: tuple[SkillInfo, ...]
    ) -> None:
        super().__init__(f"{self._MESSAGES[relation]}：{incoming.name}")
        self.relation = relation
        self.incoming = incoming
        self.existing = existing


class SkillImporter:
    """导入 Skill 到一个作用域的专属目录；``workspace_root`` 为空即系统级。"""

    def __init__(self, *, workspace_root: Path | None = None) -> None:
        self.scope = ExtensionScope(workspace_root)

    def import_directory(
        self, source_path: str, resolution: SkillImportResolution | None = None
    ) -> SkillInfo:
        """从原生选择器选中的目录复制并安装；拒绝目录内部链接，根目录链接按用户选择解析。"""

        source = self._resolve_selected_path(source_path)
        if not source.is_dir():
            raise ValueError("请选择 Skill 文件夹")

        total_bytes = 0
        file_count = 0
        entry_count = 0
        with self._staging_directory() as temporary:
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
            return self._install_staged_skill(staging, resolution)

    def import_archive(
        self, source_path: str, resolution: SkillImportResolution | None = None
    ) -> SkillInfo:
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
                with self._staging_directory() as temporary:
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
                    return self._install_staged_skill(staging, resolution)
        except zipfile.BadZipFile as exc:
            raise ValueError("无法读取 Skill ZIP 文件") from exc

    def _install_staged_skill(
        self, staging: Path, resolution: SkillImportResolution | None
    ) -> SkillInfo:
        """按严格标准校验暂存目录，判定冲突后原子安装到本作用域；配置写入失败时回滚目录。

        导入前若停用过同名 Skill，导入即重新启用（用户刚选了它，留着停用名单会让新文件不可用）。

        异常:
            ValueError: 不符合 Agent Skills 标准。
            SkillImportConflict: 有冲突，且 ``resolution`` 与判定出的关系不符（或关系为 ``identical``）。
            FileExistsError: 目标目录被另一个声明了不同名称的 Skill 占用。
        """

        with file_store.locked(self.scope.skills_config_file):
            configuration = SkillConfiguration.read(self.scope)
            root = SkillRoot(self.scope, "cosir")
            info = read_skill_metadata(staging, root, configuration)
            if info.warnings:
                raise ValueError(f"Skill 不符合标准：{'；'.join(info.warnings)}")
            relation, existing = self._relation(info)
            if relation is not None and (relation == "identical" or resolution != relation):
                log.info(
                    "configuration_skill_import_conflict",
                    extra={
                        "msg": "Skill 导入与现有同名 Skill 冲突，等用户决定",
                        "data": {
                            "scope": self.scope.key,
                            "skill_name": info.name,
                            "relation": relation,
                            "resolution": resolution,
                        },
                    },
                )
                raise SkillImportConflict(relation, info, existing)
            # 替换的是专属目录里那条同名 Skill，它的文件夹名可能与名称不同。
            replaced = next(item for item in existing if item.root == root) if relation == "replace" else None
            destination = root.directory / info.name
            if destination.exists() and (replaced is None or destination != replaced.directory):
                raise FileExistsError(f"专属目录里的「{info.name}」文件夹已被另一个 Skill 占用")
            # 旧目录先移进暂存目录（同盘、原子），新目录装好且配置写成后随暂存目录一起清掉；失败就移回来。
            backup = staging.parent / ".replaced"
            if replaced is not None:
                replaced.directory.rename(backup)
            staging.rename(destination)

            try:
                if not configuration.skill_enabled(info.name):
                    disabled = [item for item in configuration.disabled_skills if item != info.name]
                    configuration.model_copy(update={"disabled_skills": disabled}).write(self.scope)
            except Exception:
                try:
                    shutil.rmtree(destination)
                    if replaced is not None:
                        backup.rename(replaced.directory)
                except OSError:
                    log.exception(
                        "configuration_skill_import_rollback_failed",
                        extra={
                            "msg": "Skill 导入配置写入失败，且未能还原目录",
                            "data": {"scope": self.scope.key, "skill_name": info.name},
                        },
                    )
                raise

        log.info(
            "configuration_skill_imported",
            extra={
                "msg": "Skill 已导入",
                "data": {"scope": self.scope.key, "skill_name": info.name, "relation": relation},
            },
        )
        return SkillCatalog.visible_from(self.scope.workspace_root).find(info.name)

    def _relation(self, incoming: SkillInfo) -> tuple[SkillImportRelation | None, tuple[SkillInfo, ...]]:
        """导入的 Skill 与本页看得到的同名条目是什么关系；没有同名的返回 ``(None, ())``。

        ``identical`` 只看生效条目：生效的那份已经是这个内容，装进来对 Agent 没有变化；更低处恰好有一份
        相同的不算，因为装进来会改变生效的内容。
        """

        scans = SkillCatalog.visible_from(self.scope.workspace_root).scans()
        existing = tuple(
            info for scan in reversed(scans) for info in scan.skills if info.name == incoming.name
        )
        if not existing:
            return None, ()
        if SkillCatalog.merge(scans)[incoming.name].fingerprint == incoming.fingerprint:
            return "identical", existing
        own = SkillRoot(self.scope, "cosir")
        return ("replace" if any(info.root == own for info in existing) else "override"), existing

    def _staging_directory(self) -> tempfile.TemporaryDirectory[str]:
        """导入用的暂存目录：专属 Skill 目录里的点目录。

        三个约束决定了位置：与目标同盘，``rename`` 才是原子的；扫描跳过点目录，导入期间不会被当成 Skill；
        校验读暂存的 ``SKILL.md`` 走 Skill 统一读取入口，要守受保护路径——系统 ``.cosir`` 只有 ``skills/``
        可读，放在它的上一级会被拒（曾经的缺陷）。专属目录只有 cosir 读，其他工具看不到这个临时目录。
        """

        directory = SkillRoot(self.scope, "cosir").directory
        directory.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(prefix=".import-", dir=directory)

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
