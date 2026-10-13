"""系统级 Agent Skill 的发现、启停、导入和删除用例。"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.config.logging.logger import log
from app.service.configuration.file_store import ConfigurationFileStore
from app.service.skills.skill_catalog import SkillCatalog, SkillInfo, read_skill_metadata
from app.service.skills.skill_configuration import SkillConfiguration, read_skill_configuration
from app.utils.path.system_cosir import (
    system_cosir_dir,
    system_skills_config_file,
    system_skills_dir,
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

    def import_files(self, paths: list[str], contents: list[bytes]) -> SkillInfo:
        """校验并导入用户选择的本机 Skill 文件夹，目标固定为系统 Skill 目录。"""

        if not paths or len(paths) != len(contents):
            raise ValueError("导入文件列表无效")
        relative_paths = [self._relative_import_path(path) for path in paths]
        if len(relative_paths) != len(set(relative_paths)):
            raise ValueError("导入文件路径重复")
        root_names = {path.parts[0] for path in relative_paths}
        if len(root_names) != 1:
            raise ValueError("请选择单个 Skill 文件夹")

        selected_name = relative_paths[0].parts[0]
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="skill-import-", dir=self.root) as temporary:
            staging = Path(temporary) / selected_name
            staging.mkdir()
            for relative_path, content in zip(relative_paths, contents, strict=True):
                target = staging.joinpath(*relative_path.parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)

            info = read_skill_metadata(staging)
            destination = self.root / info.name
            if destination.exists():
                raise FileExistsError(f"已存在同名 Skill：{info.name}")
            staging.rename(destination)

        configuration = read_skill_configuration()
        if configuration.enabled_skills is not None:
            enabled_names = list(configuration.enabled_skills)
            if info.name not in enabled_names:
                enabled_names.append(info.name)
            try:
                self._write_configuration(SkillConfiguration(enabled_skills=enabled_names))
            except Exception:
                try:
                    shutil.rmtree(destination)
                except OSError:
                    log.exception(
                        "configuration_skill_import_rollback_failed",
                        extra={
                            "msg": "Skill 导入配置写入失败，且未能清理已复制目录",
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

    def import_archive(self, content: bytes) -> SkillInfo:
        """读取 ZIP 内的单个 Skill 目录，再复用文件夹导入的校验和安装流程。"""

        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                paths: list[str] = []
                contents: list[bytes] = []
                for item in archive.infolist():
                    if item.is_dir():
                        continue
                    relative_path = self._relative_import_path(item.filename)
                    if "__MACOSX" in relative_path.parts or relative_path.name == ".DS_Store":
                        continue
                    if relative_path.name.startswith("._"):
                        continue
                    paths.append(item.filename)
                    contents.append(archive.read(item))
        except zipfile.BadZipFile as exc:
            raise ValueError("无法读取 Skill ZIP 文件") from exc
        return self.import_files(paths, contents)

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
        """只接受浏览器目录选择器生成的相对路径，防止上传文件逃出导入目录。"""

        path = PurePosixPath(value)
        if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in value:
            raise ValueError("导入文件路径必须位于所选 Skill 文件夹内")
        return path
