"""workspace 级 ``.fileignore`` 的来源、默认初始化与合并忽略规则。

单一职责：把 ``.fileignore``（标准 gitignore 语义，位于 ``<workspace>/.cosir/``）解析为匹配器，并在
文件缺失时按空内容初始化该文件；与 workspace 根 ``.gitignore`` 合并为单一忽略来源
（:func:`load_search_ignore_rules`）。不负责目录遍历（见 ``file_walker``）。

``.fileignore`` 采用与 ``.gitignore`` 一致的标准 gitignore（gitwildmatch）语法，二者合并后任一命中即
跳过；不再支持旧版自定义目录名/相对路径/``re:`` 正则格式。

文件存在但没有任何有效规则时，语义为「不忽略任何目录」——尊重用户的显式配置。

副作用：:func:`load_fileignore_rules` 在规则文件缺失时会创建 ``<workspace>/.cosir`` 与
``.fileignore`` 并写入空内容；文件过大、行数超限或读写失败时按可用的最大信息降级并写
WARNING 日志，绝不阻断搜索链路。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config.logging.logger import log
from app.core.tools.tool_handler.search.gitignore_rules import (
    GitignoreMatcher,
    build_gitwildmatch_spec,
    load_gitignore_rules,
    valid_rule_lines,
)
from app.core.tools.tool_handler.search.ignore_matcher import IgnoreMatcher
from app.utils.cosir_paths import workspace_cosir_dir

IGNORE_FILE_NAME: str = ".fileignore"
"""workspace 级忽略规则文件名（位于 ``<workspace>/.cosir/`` 下）。"""

DEFAULT_IGNORED_DIR_NAMES: tuple[str, ...] = (
    ".git",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".venv",
    "venv",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "target",
    "out",
    ".idea",
    ".vscode",
    ".tox",
    ".eggs",
    "site-packages",
)
"""内置默认忽略目录名（17 项）；仅在无 workspace 或规则文件读写失败时使用。"""

FILEIGNORE_MAX_FILE_BYTES: int = 256 * 1024
"""规则文件读取上限（字符数）：超出部分不读入，避免异常大文件撑大内存。"""

FILEIGNORE_MAX_RULES: int = 2000
"""规则行数上限：超出的行被忽略并记 WARNING，避免规则集合被异常文件撑大。"""

_HEADER_LINES: tuple[str, ...] = (
    "# 目录忽略规则（workspace 级，标准 gitignore 语法，UTF-8 每行一条）："
    "命中的目录连同其整棵子树一并跳过。",
    "# - 以 # 起始的行为注释，空行忽略；语法与 .gitignore 一致（支持 "
    "*.log、build/、**/x、!keep 否定等）。",
    "# - 本文件与 workspace 根的 .gitignore 合并生效：任一命中即跳过。",
)


@dataclass(frozen=True)
class CompositeIgnoreMatcher:
    """组合多个 :class:`IgnoreMatcher`，任一命中即视为忽略（OR 语义）。"""

    matchers: tuple[IgnoreMatcher, ...]

    def match_dir(self, directory: Path) -> bool:
        """任一子匹配器判定目录命中即跳过整棵子树。"""

        return any(matcher.match_dir(directory) for matcher in self.matchers)

    def match_file(self, file: Path) -> bool:
        """任一子匹配器判定文件命中即跳过该文件。"""

        return any(matcher.match_file(file) for matcher in self.matchers)


def ignore_file_path(workspace_root: str | Path) -> Path:
    """返回 workspace 级忽略规则文件路径（``<workspace>/.cosir/.fileignore``）。

    参数:
        workspace_root: 工作区根目录。

    返回:
        规则文件路径；不校验存在性、不创建目录。

    异常:
        无。

    副作用:
        无（纯路径拼接；``.cosir`` 位置由 ``app.utils.cosir_paths`` 收口）。
    """

    return workspace_cosir_dir(workspace_root) / IGNORE_FILE_NAME


def default_ignore_rules() -> GitignoreMatcher:
    """返回内置默认规则（标准 gitignore 语义，相对路径基准为 None 兜底）。

    供未持有 workspace 上下文的调用方使用；持有 workspace 的调用方应改用
    :func:`load_search_ignore_rules` 读取该 workspace 的 ``.fileignore`` + ``.gitignore``。

    参数:
        无。

    返回:
        以 ``DEFAULT_IGNORED_DIR_NAMES`` 构造、``workspace_root=None`` 的 gitwildmatch 匹配器。

    异常:
        无。

    副作用:
        无（纯常量构造，不访问文件系统）。
    """

    spec = build_gitwildmatch_spec(list(DEFAULT_IGNORED_DIR_NAMES))
    return GitignoreMatcher(workspace_root=None, spec=spec)


def render_default_content() -> str:
    """渲染首次创建 ``.fileignore`` 时写入的默认文本。

    参数:
        无。

    返回:
        空字符串，表示 workspace 默认不额外忽略任何路径。

    异常:
        无。

    副作用:
        无。
    """

    return ""


def _empty_matcher(workspace_root: Path) -> GitignoreMatcher:
    """返回无任何有效规则的匹配器（对应「显式空文件 = 不忽略任何目录」语义）。"""

    return GitignoreMatcher(workspace_root=workspace_root, spec=build_gitwildmatch_spec([]))


def load_fileignore_rules(workspace_root: str | Path) -> GitignoreMatcher:
    """读取 workspace 的 ``.fileignore``；文件缺失时先创建空文件再解析。

    每次调用都重新读取文件，使规则改动无需重启即刻生效；一次遍历只需调用一次（遍历内部复用
    返回的规则对象）。采用标准 gitignore（gitwildmatch）语法，相对 workspace 根匹配。

    参数:
        workspace_root: 工作区根目录；为空或纯空白时视为无 workspace，直接返回默认规则，
            不会退化成「在进程当前目录创建规则文件」。

    返回:
        解析后的 gitwildmatch 匹配器。文件缺失且创建成功时，文件为空，返回空匹配器。

    异常:
        无：路径非法、读写失败或编码错误一律降级为默认规则并写 WARNING 日志。

    副作用:
        仅当规则文件不存在时创建 ``<workspace>/.cosir`` 目录与 ``.fileignore`` 文件；文件过大
        或行数超限时只记 WARNING，不修改文件。
    """

    root_text = str(workspace_root).strip()
    if not root_text:
        log.warning(
            "file_ignore_workspace_missing",
            extra={"msg": "workspace 根为空，本次使用内置默认忽略规则", "data": {}},
        )
        return default_ignore_rules()
    root = Path(root_text)
    path = ignore_file_path(root)
    text = _read_ignore_text(path)
    if text is None:
        return default_ignore_rules()
    text = _limit_rules(text, path)
    lines = valid_rule_lines(text)
    if not lines:
        # 缺失文件会被创建为空文件；空文件和显式空文件都表示 workspace 不额外忽略路径。
        return _empty_matcher(root)
    try:
        spec = build_gitwildmatch_spec(lines)
    except Exception as exc:
        log.warning(
            "file_ignore_parse_failed",
            extra={
                "msg": ".fileignore 解析失败，本次降级为内置默认忽略规则",
                "data": {"path": str(path), "error": str(exc), "error_type": type(exc).__name__},
            },
        )
        return default_ignore_rules()
    return GitignoreMatcher(workspace_root=root, spec=spec)


def load_search_ignore_rules(workspace_root: str | Path) -> IgnoreMatcher:
    """读取 workspace 的搜索忽略规则组合：``.fileignore`` + workspace 根 ``.gitignore``。

    供搜索引擎在遍历时一次性注入；两个文件都采用标准 gitignore 语义，合并后任一命中即跳过。
    ``.fileignore`` 缺失时创建空文件，``.gitignore`` 缺失时仅 ``.fileignore`` 生效。文件级
    规则（``*.log``）与目录级规则（``build/``）在二者之间始终按统一语义生效。

    参数:
        workspace_root: 工作区根目录。

    返回:
        组合后的 :class:`IgnoreMatcher`；仅 ``.fileignore`` 生效时直接返回其匹配器（避免无谓包装）。

    异常:
        无：子匹配器读取失败一律降级，不阻断搜索。

    副作用:
        同 :func:`load_fileignore_rules`（``.fileignore`` 缺失时创建空文件）。
    """

    file_rules = load_fileignore_rules(workspace_root)
    git = load_gitignore_rules(workspace_root)
    if git is None:
        return file_rules
    return CompositeIgnoreMatcher((file_rules, git))


def _limit_rules(text: str, path: Path) -> str:
    """把规则文本裁到 ``FILEIGNORE_MAX_RULES`` 行以内，超限时写 WARNING（带文件路径便于定位）。

    参数:
        text: 规则文件全文（可能已被 ``FILEIGNORE_MAX_FILE_BYTES`` 截断）。
        path: 规则文件路径，仅用于日志定位。

    返回:
        最多 ``FILEIGNORE_MAX_RULES`` 行的文本；未超限时原样返回。

    异常:
        无。

    副作用:
        超限时写 WARNING 日志 ``file_ignore_rules_truncated``。
    """

    lines = text.splitlines()
    if len(lines) <= FILEIGNORE_MAX_RULES:
        return text
    log.warning(
        "file_ignore_rules_truncated",
        extra={
            "msg": ".fileignore 行数超过上限，多余行已忽略",
            "data": {"path": str(path), "limit": FILEIGNORE_MAX_RULES, "lines": len(lines)},
        },
    )
    return "\n".join(lines[:FILEIGNORE_MAX_RULES])


def _read_ignore_text(path: Path) -> str | None:
    """读取规则文件文本；文件不存在时创建空文件，失败时返回 ``None``。

    不用 ``app.utils.file_utils.read_text_file`` 收口：本函数需要按上限读取并在读取失败时降级，
    而该收口是「整文件 UTF-8 读取」的薄包装，无法表达这两点。

    参数:
        path: 规则文件路径。

    返回:
        文件全文；文件缺失且创建成功（或被并发方创建）时为该文件内容，创建或读取失败时为 ``None``。

    异常:
        无：路径非法（如含 NUL 字节）、读写失败或解码失败一律降级为 ``None`` 并写 WARNING 日志。

    副作用:
        可能创建父目录与规则文件；并发创建命中的 ``FileExistsError`` 视为「他人已建」。
    """

    try:
        return _read_limited(path)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        _log_read_failure(path, exc)
        return None

    content = render_default_content()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # "x" 模式保证「仅创建」：并发方已创建时不覆盖用户可能已改写的规则。
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
    except FileExistsError:
        try:
            return _read_limited(path)
        except (OSError, ValueError) as exc:
            _log_read_failure(path, exc)
            return None
    except (OSError, ValueError) as exc:
        log.warning(
            "file_ignore_create_failed",
            extra={
                "msg": ".fileignore 创建失败，本次使用内置默认忽略规则",
                "data": {
                    "path": str(path),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            },
        )
        return None
    return content


def _read_limited(path: Path) -> str:
    """按 ``FILEIGNORE_MAX_FILE_BYTES`` 上限读取规则文件。

    参数:
        path: 规则文件路径。

    返回:
        文件文本；超过上限时只返回前 ``FILEIGNORE_MAX_FILE_BYTES`` 个字符。

    异常:
        OSError: 文件不存在或无权限等 I/O 错误（由调用方降级）。
        ValueError: 路径非法（如含 NUL 字节）或 UTF-8 解码失败（``UnicodeDecodeError``）。

    副作用:
        无（只读文件）。
    """

    with path.open("r", encoding="utf-8") as handle:
        return handle.read(FILEIGNORE_MAX_FILE_BYTES)


def _log_read_failure(path: Path, exc: Exception) -> None:
    """记录规则文件读取失败的 WARNING 日志（含异常文本与堆栈，便于定位）。

    参数:
        path: 规则文件路径。
        exc: 捕获到的异常。

    返回:
        无。

    异常:
        无。

    副作用:
        写 WARNING 日志 ``file_ignore_read_failed``，``exc_info=True`` 带出异常堆栈。
    """

    log.warning(
        "file_ignore_read_failed",
        extra={
            "msg": ".fileignore 读取失败，已降级为内置默认忽略规则",
            "data": {"path": str(path), "error": str(exc), "error_type": type(exc).__name__},
        },
        exc_info=True,
    )
