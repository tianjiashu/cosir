"""Skill 配置页的读取视图：按根目录（作用域 × 身份）分组的目录、单个 Skill 的详情。

只读投影，不写配置。谁生效、谁被覆盖、哪些是同一份的判定全部来自 ``SkillCatalog.merge``，这里只把同一份
结论按根目录摊开给用户看，前端不再推断任何合并规则。Agent 消费的目录仍走 ``SkillCatalog.list_skills``。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.core.context.system_prompt_builder import SystemPromptBuilder
from app.service.configuration.skills.catalog import (
    SKILL_FILE_NAME,
    SkillCatalog,
    SkillInfo,
    SkillRootScan,
    SkippedSkill,
    parse_skill_markdown,
    read_skill_file,
)
from app.service.configuration.skills.skill_root import SkillRoot
from app.utils.path.shared_skills import home_relative
from app.utils.token_estimator import TokenEstimator

SkillStanding = Literal["effective", "shadowed"]
"""``effective``：Agent 用的就是它；``shadowed``：被优先级更高、指纹不同的同名 Skill 覆盖。
与生效条目同指纹的是同一份，不列条目，记在生效条目的 ``also_at`` 里。"""

# 详情页的文件清单只为让用户看清 Skill 带了什么（尤其会被执行的脚本），不是文件浏览器：限深限量，
# 跳过点目录与依赖目录，超出时标记截断，完整内容可在文件夹里看。
_DETAIL_MAX_FILES = 200
_DETAIL_MAX_DEPTH = 4
_DETAIL_SKIPPED_DIRS = frozenset({"node_modules", "__pycache__"})
_BINARY_SNIFF_BYTES = 8192


@dataclass(frozen=True)
class SkillEntry:
    """分组里的一个 Skill 及其处境。

    停用名单按名称记录，所以只有 ``effective`` 的条目可以启停：对被覆盖的条目启停，改到的会是生效的
    同名 Skill。
    """

    info: SkillInfo
    standing: SkillStanding


@dataclass(frozen=True)
class SkillGroup:
    """一个根目录；目录还不存在时也列出（空），让用户知道往哪放、导入落到哪。"""

    scan: SkillRootScan
    entries: tuple[SkillEntry, ...]

    @property
    def root(self) -> SkillRoot:
        return self.scan.root

    @property
    def skipped(self) -> tuple[SkippedSkill, ...]:
        return self.scan.skipped


@dataclass(frozen=True)
class SkillCatalogView:
    """配置页的分组目录与 Agent 目录体量。

    分组按优先级从高到低：页面自己这一层在前（工作区页先列工作区，再列继承来的系统级），同一层里专属在前。
    ``agent_tokens`` 用系统提示词同一个渲染函数拼出目录文本后估算，与 Agent 实际看到的同源。
    """

    groups: tuple[SkillGroup, ...]
    agent_skills: int
    agent_tokens: int

    @classmethod
    def build(cls, workspace_root: Path | None) -> SkillCatalogView:
        """读出某个作用域配置页的视图（``None`` 为系统级）。

        异常:
            SkillConfigurationError: 某一层的配置文件无法读取或格式无效。
        """

        scans = SkillCatalog.visible_from(workspace_root).scans()
        effective = SkillCatalog.merge(scans)
        groups = tuple(
            SkillGroup(
                scan,
                tuple(
                    entry
                    for info in scan.skills
                    if (entry := _entry(info, effective[info.name])) is not None
                ),
            )
            for scan in reversed(scans)
        )
        offered = sorted(
            (info for info in effective.values() if info.offered_to_model),
            key=lambda info: info.name,
        )
        catalog_text = SystemPromptBuilder.render_skill_catalog(
            (info.name, info.description) for info in offered
        )
        return cls(groups, len(offered), TokenEstimator.estimate(catalog_text))


def _entry(info: SkillInfo, winner: SkillInfo) -> SkillEntry | None:
    """条目在分组里的处境；与生效条目同一份（同指纹）时不单列，返回 ``None``。"""

    if winner.identity == info.identity:
        # 用合并后的对象：它带着 ``overrides`` 与 ``also_at``。
        return SkillEntry(winner, "effective")
    if winner.fingerprint == info.fingerprint:
        return None
    return SkillEntry(info, "shadowed")


@dataclass(frozen=True)
class SkillFile:
    """Skill 目录里的一个文件；``path`` 相对 Skill 目录（POSIX 分隔），``binary`` 的文件不提供预览。"""

    path: str
    size: int
    binary: bool


@dataclass(frozen=True)
class SkillDetail:
    """配置页详情：正文、frontmatter 里标准两项之外的字段、目录里的文件清单与位置。

    ``metadata`` 原样转成文本展示（license、compatibility、allowed-tools 及各产品的扩展字段），
    cosir 不解释它们。``directory`` 是真实目录的绝对路径，只给「在文件夹中显示」用；给人看的是 ``location``。
    """

    info: SkillInfo
    body: str
    metadata: dict[str, str]
    files: tuple[SkillFile, ...]
    files_truncated: bool

    @property
    def directory(self) -> Path:
        return self.info.directory.resolve()

    @property
    def location(self) -> str:
        return home_relative(self.info.directory)

    @property
    def also_at(self) -> tuple[str, ...]:
        """同一份 Skill 的其他位置（给人看），多为软链或复制安装。"""

        return tuple(home_relative(directory) for directory in self.info.also_at)

    @classmethod
    def read(cls, catalog: SkillCatalog, name: str) -> SkillDetail:
        """读取一个可见 Skill 的详情，不受启用状态限制：停用的 Skill 也要能看清再决定是否启用。

        异常:
            KeyError: 没有这个名称的 Skill。
            SkillLoadError: SKILL.md 读取或解析失败（列表扫描之后文件被改坏）。
        """

        info = catalog.find(name)
        frontmatter, body = parse_skill_markdown(read_skill_file(info))
        metadata = {
            key: value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            for key, value in frontmatter.items()
            if key not in ("name", "description")
        }
        files, truncated = _list_skill_files(info.directory)
        return cls(info, body, metadata, files, truncated)


def _list_skill_files(directory: Path) -> tuple[tuple[SkillFile, ...], bool]:
    """列出 Skill 目录里的文件（SKILL.md 在前，其余按目录树顺序）及是否因数量上限截断。

    不跟随目录软链；文件软链照常列出，预览时由 ``read_skill_file`` 按真实路径约束。
    """

    root = directory.resolve(strict=True)
    files: list[SkillFile] = []
    for current, directory_names, file_names in os.walk(root):
        relative_directory = Path(current).relative_to(root)
        directory_names[:] = (
            sorted(
                item
                for item in directory_names
                if not item.startswith(".") and item not in _DETAIL_SKIPPED_DIRS
            )
            if len(relative_directory.parts) < _DETAIL_MAX_DEPTH
            else []
        )
        for file_name in sorted(file_names):
            if file_name.startswith("."):
                continue
            if len(files) >= _DETAIL_MAX_FILES:
                return _skill_md_first(files), True
            path = Path(current) / file_name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            files.append(
                SkillFile((relative_directory / file_name).as_posix(), size, _looks_binary(path))
            )
    return _skill_md_first(files), False


def _skill_md_first(files: list[SkillFile]) -> tuple[SkillFile, ...]:
    return tuple(sorted(files, key=lambda item: item.path != SKILL_FILE_NAME))


def _looks_binary(path: Path) -> bool:
    """按开头是否有 NUL 字节粗判二进制；读不了的也当二进制，不提供预览。"""

    try:
        with path.open("rb") as file:
            return b"\0" in file.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return True
