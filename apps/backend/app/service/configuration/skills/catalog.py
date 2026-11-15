"""发现系统级与 workspace 级 Skill 目录里的 Agent Skills，按层合并后按需读取说明或配套资源。

每个作用域有两种身份的 Skill 目录（cosir 专属 ``.cosir/skills`` 与共享 ``.agents/skills``，见
``skills/skill_root``），对 Agent 完全平等。同名时先按指纹（``SKILL.md`` 内容）去重：软链或复制来的同一份
只算一个；指纹不同才是真冲突，按根目录优先级取一份（workspace 压过系统级，同一作用域里专属压过共享）。
不读其他 Agent 产品各自的目录（``.claude/skills`` 等）：成熟产品也只读自己那一套，多来源会带来来源开关、
跨来源去重这一串问题；想用别处的 Skill，导入或自己软链进来。

每次构造都现读目录与配置，不缓存：外部工具随时安装或卸载 Skill，workspace Skill 也会随 git 拉取
更新，配置文件与目录本身就是唯一事实源。合并规则只在 ``SkillCatalog.merge`` 一处；Agent 看到的目录
与配置页的分组视图（``skills/configuration_view``）都由它得出。

校验按 Agent Skills 标准给客户端的宽松建议：缺 description 或 frontmatter 解析不了才跳过（并把原因
交给界面），名称不合规范、与目录名不一致只记警告、照样加载，以 frontmatter 的 ``name`` 为准。
cosir 自己导入时仍按严格标准拒绝（见 ``SkillConfigurationService``）：那是作者写 Skill 时的检查。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.permission.protected_paths import ProtectedPaths
from app.service.configuration.extension_scope import ExtensionScope
from app.service.configuration.skills.configuration import SkillConfiguration
from app.service.configuration.skills.skill_root import SkillRoot

_SKILL_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SKILL_NAME_MAX_CHARS = 64
_SKILL_DESCRIPTION_MAX_CHARS = 1024
SKILL_FILE_NAME = "SKILL.md"
# Agent Skills 标准约定可执行脚本放在 ``scripts/``；「含脚本」只认它，其他位置的可执行文件不算。
_SCRIPTS_DIR_NAME = "scripts"
_MAX_RESOURCE_BYTES = 1_000_000
# 交给模型的 Skill 正文预算：与工具结果进入模型通道的上限同一个数，并预留目录说明与截断提示，
# 这样 ``skill`` 工具加载与用户在输入框选中同一个 Skill，模型拿到的内容一致。
_MODEL_BODY_CHARS = Constant.Tools.MAX_OUTPUT_CHARS - 1024


@dataclass(frozen=True)
class SkillInfo:
    """一个 Skill 的元数据、所在根目录与启用状态。

    ``fingerprint``：``SKILL.md`` 内容的哈希（统一换行），同名同指纹即同一份 Skill。
    ``overrides``：它遮住了优先级更低的、指纹不同的同名 Skill。``also_at``：同一份（同指纹）在优先级更低的
    根目录里的其他位置，合并后只列这一条。
    ``manual_only``：作者声明了 ``disable-model-invocation: true``（Claude Code 的扩展字段），只给用户
    手动选用，不进模型看到的目录。``has_scripts``：带 ``scripts/`` 目录，启用前值得看一眼会执行什么。
    """

    name: str
    description: str
    directory: Path
    enabled: bool
    root: SkillRoot
    fingerprint: str
    overrides: bool = False
    also_at: tuple[Path, ...] = ()
    warnings: tuple[str, ...] = ()
    manual_only: bool = False
    has_scripts: bool = False

    @property
    def scope(self) -> ExtensionScope:
        return self.root.scope

    @property
    def identity(self) -> tuple[str, str, str]:
        """条目身份（作用域 + 身份 + 名称）：合并会产生新对象，比较是不是同一条只认它。"""

        return (self.scope.key, self.root.kind, self.name)

    @property
    def skill_file(self) -> Path:
        """该 Skill 的 ``SKILL.md`` 绝对路径；用户输入的 ``<skill>`` 胶囊把它写给模型按需读取。"""

        return (self.directory / SKILL_FILE_NAME).resolve()

    @property
    def offered_to_model(self) -> bool:
        """是否列进模型看到的 Skill 目录（系统提示词、``skill`` 工具的 list、配置页的目录体量）。"""

        return self.enabled and not self.manual_only


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
                f"{directory / SKILL_FILE_NAME}; read the rest from that file.]"
            )
        return (
            f'Skill "{self.info.name}". Its files are in {directory}; relative paths in these '
            "instructions are relative to that directory.\n\n"
            f"{body}"
        )


@dataclass(frozen=True)
class SkippedSkill:
    """Skill 目录里一个无法加载的子目录：目录名与原因，交给配置页展示。"""

    directory: str
    reason: str


class SkillLoadError(ValueError):
    """Skill 不存在、格式无效或请求的资源无法读取。"""


@dataclass(frozen=True)
class SkillRootScan:
    """一个根目录的扫描结果：能加载的 Skill 与加载不了的子目录。"""

    root: SkillRoot
    skills: tuple[SkillInfo, ...]
    skipped: tuple[SkippedSkill, ...]


class SkillCatalog:
    """按根目录合并的 Skill 目录（优先级见 ``SkillRoot.visible_from``）。"""

    def __init__(self, roots: tuple[SkillRoot, ...]) -> None:
        self._roots = roots

    @classmethod
    def visible_from(cls, workspace_root: str | Path | None) -> SkillCatalog:
        """某个工作区（``None`` 为系统级配置页）看得到的 Skill 目录。"""

        return cls(SkillRoot.visible_from(workspace_root))

    def list_skills(self) -> list[SkillInfo]:
        """返回合并后的有效 Skill，按名称排序。

        异常:
            SkillConfigurationError: 某一层的配置文件无法读取或格式无效。
        """

        merged = self.merge(self.scans())
        return [merged[name] for name in sorted(merged)]

    def scans(self) -> list[SkillRootScan]:
        """各根目录的扫描结果，按优先级从低到高排列；目录不存在时为空。

        异常:
            SkillConfigurationError: 某一层的配置文件无法读取或格式无效。
        """

        # 停用名单按作用域：同一作用域的两个根目录共用一次读取。
        configurations = {root.scope.key: root.scope for root in self._roots}
        read = {key: SkillConfiguration.read(scope) for key, scope in configurations.items()}
        return [_scan_root(root, read[root.scope.key]) for root in self._roots]

    @staticmethod
    def merge(scans: Sequence[SkillRootScan]) -> dict[str, SkillInfo]:
        """合并成「名称 → 生效条目」；``scans`` 须从低到高排列。

        优先级最高的那份生效；更低处同指纹的是同一份，记进 ``also_at``；更低处有指纹不同的，生效条目
        记为 ``overrides``。
        """

        by_name: dict[str, list[SkillInfo]] = {}
        for scan in scans:
            for info in scan.skills:
                by_name.setdefault(info.name, []).append(info)
        merged: dict[str, SkillInfo] = {}
        for name, infos in by_name.items():
            *lower, winner = infos
            merged[name] = replace(
                winner,
                overrides=any(info.fingerprint != winner.fingerprint for info in lower),
                also_at=tuple(
                    info.directory for info in reversed(lower) if info.fingerprint == winner.fingerprint
                ),
            )
        return merged

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

        仅限手动的 Skill 也能加载：用户在输入框里选了它，模型会按名称来取。

        异常:
            SkillLoadError: 不存在、已停用或文件无法读取。
        """

        info = self._enabled(name)
        _, body = parse_skill_markdown(read_skill_file(info))
        return SkillDocument(info, body)

    def load_resource(self, name: str, resource_path: str) -> tuple[SkillInfo, str]:
        """读取已启用 Skill 目录内的一个配套文本文件，连同它所属的 Skill。"""

        info = self._enabled(name)
        return info, read_skill_file(info, resource_path)

    def _enabled(self, name: str) -> SkillInfo:
        try:
            info = self.find(name)
        except KeyError:
            raise SkillLoadError(f"未找到 Skill：{name}") from None
        if not info.enabled:
            raise SkillLoadError(f"Skill 已禁用：{name}")
        return info


def _scan_root(root: SkillRoot, configuration: SkillConfiguration) -> SkillRootScan:
    """列出一个根目录里的 Skill：只看一层子目录，跳过点目录和没有 ``SKILL.md`` 的目录。

    只扫一层：其他产品把自己管理的内容放在点目录或嵌套目录里（如 Codex 的 ``.system/``），递归会把
    它们误当成用户的 Skill。目录是进程外输入（其他工具也往里写）：整个目录读不了时记日志并当作空目录，
    不让一个坏目录拖垮整个列表。两个子目录声明同一个 name（name 与目录名不一致时可能发生）时保留排序
    在前的，后者记为跳过。
    """

    directory = root.directory
    if not directory.is_dir():
        return SkillRootScan(root, (), ())
    skills: list[SkillInfo] = []
    skipped: list[SkippedSkill] = []
    try:
        entries = sorted(directory.iterdir(), key=lambda path: path.name.casefold())
    except OSError as exc:
        log.warning(
            "skill_directory_unreadable",
            extra={
                "msg": "Skill 目录无法读取，已按空目录处理",
                "data": {"scope": root.scope.key, "kind": root.kind, "error_type": type(exc).__name__},
            },
        )
        return SkillRootScan(root, (), (SkippedSkill(".", "目录无法读取"),))
    for entry in entries:
        if (
            entry.name.startswith(".")
            or not entry.is_dir()
            or not (entry / SKILL_FILE_NAME).is_file()
        ):
            continue
        try:
            info = read_skill_metadata(entry, root, configuration)
        except SkillLoadError as exc:
            skipped.append(SkippedSkill(entry.name, str(exc)))
            continue
        if any(item.name == info.name for item in skills):
            skipped.append(SkippedSkill(entry.name, f"与另一个子目录里的 Skill 重名：{info.name}"))
            continue
        skills.append(info)
    if skipped:
        log.warning(
            "skill_metadata_invalid",
            extra={
                "msg": "Skill 目录里有无法加载的 Skill，已略过",
                "data": {
                    "scope": root.scope.key,
                    "kind": root.kind,
                    "skipped": [
                        {"directory": item.directory, "reason": item.reason} for item in skipped
                    ],
                },
            },
        )
    return SkillRootScan(root, tuple(skills), tuple(skipped))

def read_skill_file(info: SkillInfo, resource_path: str | None = None) -> str:
    """读取 Skill 目录内的一个 UTF-8 文件（缺省为 ``SKILL.md``），Agent 与配置页共用这一个入口。

    目录按真实路径约束，软链进来的 Skill 也成立；是否要求已启用由调用方决定。

    异常:
        SkillLoadError: 路径越出 Skill 目录、文件不存在、命中受保护路径、超过 1 MB 或不是 UTF-8 文本。
    """

    relative_path = Path(resource_path) if resource_path else Path(SKILL_FILE_NAME)
    if relative_path.is_absolute() or not relative_path.parts:
        raise SkillLoadError("Skill 资源路径必须是目录内的相对路径")
    try:
        skill_directory = info.directory.resolve(strict=True)
        target = (skill_directory / relative_path).resolve(strict=True)
    except FileNotFoundError:
        raise SkillLoadError("Skill 目录内没有这个文件") from None
    if not target.is_relative_to(skill_directory) or not target.is_file():
        raise SkillLoadError("Skill 资源必须是 Skill 目录内的文件")
    return _read_skill_text(target)


def _read_skill_text(path: Path) -> str:
    """Skill 文件读取的唯一入口：受保护路径、大小上限与编码在这里一处守住。

    Skill 目录可以是指向任意位置的软链；指进 cosir 自身数据（``.env``、模型 Key、业务库）的一律拒绝，
    规则与文件工具同一份事实（``ProtectedPaths``）。
    """

    if ProtectedPaths.current().denies_read(path):
        raise SkillLoadError("Skill 文件位于受保护目录，无法读取")
    try:
        if path.stat().st_size > _MAX_RESOURCE_BYTES:
            raise SkillLoadError("Skill 文件超过 1 MB，无法加载")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillLoadError("Skill 文件无法读取") from exc


def read_skill_metadata(
    directory: Path,
    root: SkillRoot,
    configuration: SkillConfiguration,
) -> SkillInfo:
    """解析 Skill 目录里的 frontmatter，按标准的宽松规则校验。

    返回的 ``warnings`` 记录不合标准但不妨碍使用的问题；需要严格校验的调用方（导入）自行据此拒绝。

    异常:
        SkillLoadError: SKILL.md 读不了、frontmatter 解析不了、缺 description，或 name 不是单行文本。
    """

    content = _read_skill_text(directory / SKILL_FILE_NAME)
    metadata, _ = parse_skill_markdown(content)
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillLoadError("Skill description 不能为空")

    warnings: list[str] = []
    name = metadata.get("name")
    if name is None:
        name = directory.name
        warnings.append("缺少 name，已按目录名使用")
    elif not isinstance(name, str) or not _is_single_line_name(name):
        # 名称会出现在 URL 路径、胶囊标签与模型目录里，斜杠、换行与首尾空白都会破坏这些位置。
        raise SkillLoadError("Skill name 必须是不含斜杠的单行文本")
    else:
        if len(name) > _SKILL_NAME_MAX_CHARS or not _SKILL_NAME_PATTERN.fullmatch(name):
            warnings.append("name 不符合标准：只能用小写字母、数字和单个连字符，最长 64 个字符")
        if name != directory.name:
            warnings.append(f"name 与目录名「{directory.name}」不一致")
    if len(description) > _SKILL_DESCRIPTION_MAX_CHARS:
        warnings.append("description 超过标准上限 1024 个字符")
    return SkillInfo(
        name=name,
        description=description.strip(),
        directory=directory,
        enabled=configuration.skill_enabled(name),
        root=root,
        fingerprint=_fingerprint(content),
        warnings=tuple(warnings),
        manual_only=metadata.get("disable-model-invocation") is True,
        has_scripts=(directory / _SCRIPTS_DIR_NAME).is_dir(),
    )


def _fingerprint(content: str) -> str:
    """``SKILL.md`` 的内容指纹：统一换行后取哈希，Windows 复制来的同一份（CRLF）也算相同。

    只看 ``SKILL.md``：它是 Agent 读到的说明本身，扫描时本来就要读；随附文件不同而说明相同的情况
    很少，按同一份处理可以接受。
    """

    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _is_single_line_name(name: str) -> bool:
    return (
        bool(name)
        and name == name.strip()
        and name.isprintable()
        and not any(separator in name for separator in "/\\")
    )


def parse_skill_markdown(content: str) -> tuple[dict[str, object], str]:
    """把 SKILL.md 拆成 frontmatter 对象与去掉它之后的 Markdown 正文。

    异常:
        SkillLoadError: 缺少 frontmatter、YAML 无效或不是对象。
    """

    frontmatter, body = _split_frontmatter(content)
    try:
        metadata = yaml.safe_load(frontmatter)
    except yaml.YAMLError as exc:
        raise SkillLoadError("SKILL.md 的 YAML frontmatter 格式无效") from exc
    if not isinstance(metadata, dict):
        raise SkillLoadError("SKILL.md frontmatter 必须是对象")
    return {str(key): value for key, value in metadata.items()}, body


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
