"""发现本机 Agent Skills，并按需读取其说明或配套资源。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.config.logging.logger import log
from app.core.tool.handler.skill.skill_configuration import SkillConfiguration, read_skill_configuration
from app.utils.path.system_cosir import system_skills_dir

_SKILL_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SKILL_FILE_NAME = "SKILL.md"
_MAX_RESOURCE_BYTES = 1_000_000


@dataclass(frozen=True)
class SkillInfo:
    """系统 Skill 目录中的 Agent 元数据和启用状态。"""

    name: str
    description: str
    directory: Path
    enabled: bool


class SkillLoadError(ValueError):
    """Skill 不存在、格式无效或请求的资源无法读取。"""


class SkillCatalog:
    """读取系统级 Skill 目录，并关联系统配置中的启用状态。"""

    def __init__(self) -> None:
        self._root = system_skills_dir()
        self._configuration = read_skill_configuration()

    def list_skills(self) -> list[SkillInfo]:
        """返回有效 Skill 清单；元数据错误只跳过对应目录并记录日志。"""

        found: dict[str, SkillInfo] = {}
        if not self._root.is_dir():
            return []
        resolved_root = self._root.resolve(strict=True)
        for directory in sorted(self._root.iterdir(), key=lambda path: path.name.casefold()):
            if not directory.is_dir():
                continue
            if not directory.resolve(strict=True).is_relative_to(resolved_root):
                log.warning(
                    "skill_directory_outside_scope",
                    extra={
                        "msg": "Skill 目录指向系统目录之外，已略过",
                        "data": {"directory": directory.name},
                    },
                )
                continue
            try:
                info = read_skill_metadata(directory, self._configuration)
            except SkillLoadError as exc:
                log.warning(
                    "skill_metadata_invalid",
                    extra={
                        "msg": "Skill 元数据无效，已从目录中略过",
                        "data": {"directory": directory.name, "error": str(exc)},
                    },
                )
                continue
            found[info.name] = info
        return [found[name] for name in sorted(found)]

    def load(self, name: str, resource_path: str | None = None) -> tuple[SkillInfo, str]:
        """加载 Skill 正文或其目录内的一个配套文本资源。"""

        info = next((item for item in self.list_skills() if item.name == name), None)
        if info is None:
            raise SkillLoadError(f"未找到 Skill：{name}")
        if not info.enabled:
            raise SkillLoadError(f"Skill 已禁用：{name}")

        content = self._read_resource(info, resource_path)
        if resource_path is None:
            content = _markdown_body(content)
        return info, content

    def read_document(self, name: str) -> tuple[SkillInfo, str]:
        """读取配置界面的正文预览，不受 Agent 使用时的启用状态限制。"""

        info = next((item for item in self.list_skills() if item.name == name), None)
        if info is None:
            raise KeyError(name)
        return info, _markdown_body(self._read_resource(info, None))

    def _read_resource(self, info: SkillInfo, resource_path: str | None) -> str:
        """读取一个受 Skill 目录约束的 UTF-8 文件。"""

        trusted_root = self._root.resolve()
        skill_directory = info.directory.resolve(strict=True)
        if not skill_directory.is_relative_to(trusted_root):
            raise SkillLoadError("Skill 目录不能指向作用域目录之外")
        relative_path = Path(resource_path) if resource_path else Path(_SKILL_FILE_NAME)
        if relative_path.is_absolute() or not relative_path.parts:
            raise SkillLoadError("Skill 资源路径必须是目录内的相对路径")
        target = (info.directory / relative_path).resolve(strict=True)
        if not target.is_relative_to(skill_directory) or not target.is_file():
            raise SkillLoadError("Skill 资源必须是 Skill 目录内的文件")
        if target.stat().st_size > _MAX_RESOURCE_BYTES:
            raise SkillLoadError("Skill 文件超过 1 MB，无法加载")
        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SkillLoadError("Skill 文件无法读取") from exc

        return content


def read_skill_metadata(
    directory: Path,
    configuration: SkillConfiguration | None = None,
) -> SkillInfo:
    """解析并校验 Skill 目录中的标准 frontmatter 元数据。"""

    manifest = directory / _SKILL_FILE_NAME
    try:
        content = manifest.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillLoadError("缺少可读取的 SKILL.md") from exc
    frontmatter, _ = _split_frontmatter(content)
    try:
        metadata = yaml.safe_load(frontmatter)
    except yaml.YAMLError as exc:
        raise SkillLoadError("SKILL.md 的 YAML frontmatter 格式无效") from exc
    if not isinstance(metadata, dict):
        raise SkillLoadError("SKILL.md frontmatter 必须是对象")
    name = metadata.get("name")
    description = metadata.get("description")
    if not isinstance(name, str) or len(name) > 64 or not _SKILL_NAME_PATTERN.fullmatch(name):
        raise SkillLoadError("Skill name 必须由小写字母、数字和单个连字符组成")
    if name != directory.name:
        raise SkillLoadError("Skill name 必须与目录名一致")
    if not isinstance(description, str) or not description.strip():
        raise SkillLoadError("Skill description 不能为空")
    active_configuration = configuration or read_skill_configuration()
    return SkillInfo(
        name,
        description.strip(),
        directory,
        active_configuration.is_enabled(name),
    )


def _split_frontmatter(content: str) -> tuple[str, str]:
    """拆分以 YAML 分隔线开头的 SKILL.md。"""

    if not content.startswith("---\n"):
        raise SkillLoadError("SKILL.md 必须以 YAML frontmatter 开始")
    lines = content.splitlines(keepends=True)
    end_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.rstrip("\r\n") == "---"),
        None,
    )
    if end_index is None:
        raise SkillLoadError("SKILL.md 的 YAML frontmatter 缺少结束分隔线")
    frontmatter = "".join(lines[1:end_index])
    body = "".join(lines[end_index + 1 :]).lstrip("\r\n")
    return frontmatter, body


def _markdown_body(content: str) -> str:
    """返回去除元数据后的 Markdown 正文。"""

    _, body = _split_frontmatter(content)
    return body
