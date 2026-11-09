"""发现系统级与 workspace 级 Agent Skills，按分层合并后按需读取说明或配套资源。

每次构造都现读目录与启用名单，不缓存：workspace Skill 可能被手工放入或随 git 拉取更新，
配置文件与目录本身就是唯一事实源。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from app.config.constant import Constant
from app.config.logging.logger import log
from app.service.configuration.extension_scope import ExtensionScope
from app.service.configuration.skill_configuration import (
    SkillConfiguration,
    read_skill_configuration,
)

_SKILL_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SKILL_FILE_NAME = "SKILL.md"
_MAX_RESOURCE_BYTES = 1_000_000
# 交给模型的 Skill 正文预算：与工具结果进入模型通道的上限同一个数，并预留目录说明与截断提示，
# 这样 ``skill`` 工具加载与用户在输入框选中同一个 Skill，模型拿到的内容一致。
_MODEL_BODY_CHARS = Constant.Tools.MAX_OUTPUT_CHARS - 1024


@dataclass(frozen=True)
class SkillInfo:
    """一个 Skill 的元数据、所属作用域与启用状态。

    ``overrides`` 表示它是 workspace 层的条目，并且遮住了系统层的同名 Skill。
    """

    name: str
    description: str
    directory: Path
    enabled: bool
    scope: ExtensionScope
    overrides: bool = False

    @property
    def skill_file(self) -> Path:
        """该 Skill 的 ``SKILL.md`` 绝对路径；用户输入的 ``<skill>`` 胶囊把它写给模型按需读取。"""

        return (self.directory / _SKILL_FILE_NAME).resolve()


@dataclass(frozen=True)
class SkillDocument:
    """一个 Skill 的 ``SKILL.md`` 正文（已去掉 frontmatter）。"""

    info: SkillInfo
    body: str

    def for_model(self) -> str:
        """给模型的正文：先说明 Skill 目录（正文里的相对路径以它为基准），
        超出预算时截断并指明原文位置。
        """

        directory = self.info.directory.resolve()
        body = self.body
        if len(body) > _MODEL_BODY_CHARS:
            body = (
                f"{body[:_MODEL_BODY_CHARS]}\n\n[Truncated. The full instructions are in "
                f"{directory / _SKILL_FILE_NAME}; read the rest from that file.]"
            )
        return (
            f'Skill "{self.info.name}". Its files are in {directory}; relative paths in these '
            "instructions are relative to that directory.\n\n"
            f"{body}"
        )


class SkillLoadError(ValueError):
    """Skill 不存在、格式无效或请求的资源无法读取。"""


class SkillCatalog:
    """按层合并的 Skill 目录：后面的层覆盖前面的层里同名的 Skill。"""

    def __init__(self, layers: tuple[ExtensionScope, ...]) -> None:
        self._layers = layers

    @classmethod
    def visible_from(cls, workspace_root: str | Path | None) -> SkillCatalog:
        """某个工作区（``None`` 为系统级配置页）看得到的 Skill 目录。"""

        return cls(ExtensionScope.visible_layers(workspace_root))

    def list_skills(self) -> list[SkillInfo]:
        """返回合并后的有效 Skill，按名称排序；元数据错误只跳过对应目录并记录日志。

        异常:
            SkillConfigurationError: 某一层的启用名单文件无法读取或格式无效。
        """

        merged: dict[str, SkillInfo] = {}
        for scope in self._layers:
            configuration = read_skill_configuration(scope)
            for info in _scan_scope(scope, configuration):
                merged[info.name] = replace(info, overrides=info.name in merged)
        return [merged[name] for name in sorted(merged)]

    @staticmethod
    def workspace_defines(workspace_root: str | Path, name: str) -> bool:
        """工作区自己的目录里是否有这个名称的有效 Skill（不论启用与否，都会遮住系统级同名项）。

        不读启用名单：名单文件损坏也不影响判断覆盖关系。
        """

        scope = ExtensionScope.workspace(workspace_root)
        return any(info.name == name for info in _scan_scope(scope, SkillConfiguration()))

    def find(self, name: str) -> SkillInfo:
        """按名称取可见 Skill（不论是否启用）。

        异常:
            KeyError: 没有这个名称的 Skill。
        """

        info = next((item for item in self.list_skills() if item.name == name), None)
        if info is None:
            raise KeyError(name)
        return info

    def load_enabled(self, name: str) -> SkillDocument:
        """读取一个已启用 Skill 的正文，供 Agent 使用。

        异常:
            SkillLoadError: 不存在、已停用或文件无法读取。
        """

        info = self._enabled(name)
        return SkillDocument(info, _markdown_body(_read_resource(info, None)))

    def load_resource(self, name: str, resource_path: str) -> tuple[SkillInfo, str]:
        """读取已启用 Skill 目录内的一个配套文本文件，连同它所属的 Skill。"""

        info = self._enabled(name)
        return info, _read_resource(info, resource_path)

    def read_document(self, name: str) -> SkillDocument:
        """读取配置界面的正文预览，不受启用状态限制。

        异常:
            KeyError: 没有这个名称的 Skill。
        """

        info = self.find(name)
        return SkillDocument(info, _markdown_body(_read_resource(info, None)))

    def _enabled(self, name: str) -> SkillInfo:
        try:
            info = self.find(name)
        except KeyError:
            raise SkillLoadError(f"未找到 Skill：{name}") from None
        if not info.enabled:
            raise SkillLoadError(f"Skill 已禁用：{name}")
        return info


def _scan_scope(scope: ExtensionScope, configuration: SkillConfiguration) -> list[SkillInfo]:
    """列出一个作用域目录里的有效 Skill；指向目录之外的子目录与元数据无效的目录被跳过。"""

    root = scope.skills_dir
    if not root.is_dir():
        return []
    resolved_root = root.resolve(strict=True)
    found: list[SkillInfo] = []
    for directory in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
        if not directory.is_dir():
            continue
        if not directory.resolve(strict=True).is_relative_to(resolved_root):
            log.warning(
                "skill_directory_outside_scope",
                extra={
                    "msg": "Skill 目录指向作用域目录之外，已略过",
                    "data": {"scope": scope.key, "directory": directory.name},
                },
            )
            continue
        try:
            found.append(read_skill_metadata(directory, scope, configuration))
        except SkillLoadError as exc:
            log.warning(
                "skill_metadata_invalid",
                extra={
                    "msg": "Skill 元数据无效，已从目录中略过",
                    "data": {"scope": scope.key, "directory": directory.name, "error": str(exc)},
                },
            )
    return found


def _read_resource(info: SkillInfo, resource_path: str | None) -> str:
    """读取一个受 Skill 目录约束的 UTF-8 文件。"""

    trusted_root = info.scope.skills_dir.resolve()
    skill_directory = info.directory.resolve(strict=True)
    if not skill_directory.is_relative_to(trusted_root):
        raise SkillLoadError("Skill 目录不能指向作用域目录之外")
    relative_path = Path(resource_path) if resource_path else Path(_SKILL_FILE_NAME)
    if relative_path.is_absolute() or not relative_path.parts:
        raise SkillLoadError("Skill 资源路径必须是目录内的相对路径")
    try:
        target = (info.directory / relative_path).resolve(strict=True)
    except FileNotFoundError:
        raise SkillLoadError("Skill 目录内没有这个文件") from None
    if not target.is_relative_to(skill_directory) or not target.is_file():
        raise SkillLoadError("Skill 资源必须是 Skill 目录内的文件")
    if target.stat().st_size > _MAX_RESOURCE_BYTES:
        raise SkillLoadError("Skill 文件超过 1 MB，无法加载")
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillLoadError("Skill 文件无法读取") from exc


def read_skill_metadata(
    directory: Path,
    scope: ExtensionScope,
    configuration: SkillConfiguration,
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
    return SkillInfo(
        name=name,
        description=description.strip(),
        directory=directory,
        enabled=configuration.is_enabled(name),
        scope=scope,
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
