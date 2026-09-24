"""workspace 级目录忽略规则的来源与解析（``<workspace>/.cosir/.fileignore``）。

单一职责：把 ``.fileignore`` 文本解析为可判定的 :class:`IgnoreRules`，并在文件缺失时按默认规则
初始化该文件。不负责目录遍历与过滤（见 ``file_walker``），也不负责系统级 ``.cosir``。

文件格式（UTF-8，每行一条规则）::

    # 以 "#" 开头的整行是注释，空行忽略
    node_modules
    .cosir/tool-artifacts

``node_modules`` 不含 "/"，按目录名匹配任意层级；``.cosir/tool-artifacts`` 含 "/"，按相对
workspace 根的目录路径匹配（连同其整棵子树）。**行尾不支持注释**：规则行除首尾空白外的全部
字符都参与匹配。目录名规则大小写敏感，相对路径规则按平台口径比较（Windows 不区分大小写）。

文件存在但没有任何有效规则时，语义为「不忽略任何目录」——尊重用户的显式配置。

副作用：:func:`load_ignore_rules` 在规则文件缺失时会创建 ``<workspace>/.cosir`` 与
``.fileignore`` 并写入默认规则；文件过大、行数超限或读写失败时按可用的最大信息降级并写
WARNING 日志，绝不阻断搜索链路。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.config.logging.logger import log
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
"""内置默认忽略目录名（17 项）；仅在规则文件缺失（新建时写入）或降级时使用。"""

_MAX_FILE_BYTES: int = 256 * 1024
"""规则文件读取上限（字符数）：超出部分不读入，避免异常大文件撑大内存。"""

_MAX_RULES: int = 2000
"""规则行数上限：超出的行被忽略并记 WARNING，避免规则集合被异常文件撑大。"""

_HEADER_LINES: tuple[str, ...] = (
    "# 目录忽略规则（workspace 级，UTF-8 每行一条）：命中的目录连同其整棵子树一并跳过。",
    "# - 以 # 起始的行为注释，空行忽略；行尾不支持注释；文件内没有任何规则表示不忽略任何目录。",
    '# - 不含 "/" 的规则按目录名匹配（任意层级，大小写敏感）；',
    '#   含 "/" 的规则相对 workspace 根匹配（大小写按平台口径）。',
)


@dataclass(frozen=True)
class IgnoreRules:
    """一次目录遍历使用的忽略规则（不可变值对象）。

    ``names`` 与 ``paths`` 的分工直接映射文件格式：前者按目录名匹配任意层级，后者相对
    ``workspace_root`` 匹配；任一命中都跳过该目录及其整棵子树。
    """

    names: frozenset[str] = frozenset()
    paths: frozenset[str] = frozenset()
    workspace_root: Path | None = None

    def ignores_dir(self, directory: Path) -> bool:
        """判断目录是否命中忽略规则（命中即整棵子树都不遍历）。

        目录名规则逐字符比较（大小写敏感）；相对路径规则按平台口径归一比较（Windows 不区分
        大小写）。

        参数:
            directory: 待判定的目录路径。

        返回:
            命中目录名规则，或 ``workspace_root`` 已知且目录在其内并命中相对路径规则时返回
            ``True``；否则 ``False``。

        异常:
            无：目录不在 ``workspace_root`` 之下（如越界只读根）时退化为只按目录名判定。

        副作用:
            无（只做字符串与路径比较）。
        """

        if directory.name in self.names:
            return True
        if not self.paths or self.workspace_root is None:
            return False
        try:
            relative = directory.relative_to(self.workspace_root).as_posix()
        except ValueError:
            return False
        normalized = os.path.normcase(relative)
        return any(os.path.normcase(rule) == normalized for rule in self.paths)


def default_ignore_rules() -> IgnoreRules:
    """返回内置默认规则（仅目录名匹配，不含 workspace 相对路径规则）。

    供未持有 workspace 上下文的调用方使用；持有 workspace 的调用方应改用
    :func:`load_ignore_rules` 读取该 workspace 的 ``.fileignore``。

    参数:
        无。

    返回:
        以 ``DEFAULT_IGNORED_DIR_NAMES`` 构造、``workspace_root=None`` 的规则对象。

    异常:
        无。

    副作用:
        无（纯常量构造，不访问文件系统）。
    """

    return IgnoreRules(names=frozenset(DEFAULT_IGNORED_DIR_NAMES))


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


def parse_ignore_rules(text: str, *, workspace_root: Path | None = None) -> IgnoreRules:
    """把 ``.fileignore`` 文本解析为 :class:`IgnoreRules`。

    纯函数：不访问文件系统、不记日志、不做行数限制——行数限制与日志由
    :func:`load_ignore_rules` 承担，因为只有它持有文件路径（日志需要可定位的上下文）。

    参数:
        text: 规则文件全文。
        workspace_root: 相对路径规则的匹配基准；为 ``None`` 时这些规则不会被命中。

    返回:
        解析后的规则对象；没有有效规则时两个集合均为空。

    异常:
        无。

    副作用:
        无。
    """

    names: set[str] = set()
    paths: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # 统一分隔符并去掉首尾 "/"：``build/`` 与 ``/build`` 等价于 ``build``。
        rule = line.replace("\\", "/").strip("/")
        if not rule:
            continue
        if "/" in rule:
            paths.add(rule)
        else:
            names.add(rule)
    return IgnoreRules(
        names=frozenset(names),
        paths=frozenset(paths),
        workspace_root=workspace_root,
    )


def render_default_content() -> str:
    """渲染首次创建 ``.fileignore`` 时写入的默认文本（注释头 + 默认目录名）。

    参数:
        无。

    返回:
        以换行结尾的默认文件内容；规则来源为 ``DEFAULT_IGNORED_DIR_NAMES`` 单一事实源。

    异常:
        无。

    副作用:
        无。
    """

    lines = [*_HEADER_LINES, "", *DEFAULT_IGNORED_DIR_NAMES]
    return "\n".join(lines) + "\n"


def load_ignore_rules(workspace_root: str | Path) -> IgnoreRules:
    """读取 workspace 的 ``.fileignore``；文件缺失时先创建默认文件再解析。

    每次调用都重新读取文件，使规则改动无需重启即刻生效；一次遍历只需调用一次（遍历内部复用
    返回的规则对象）。

    参数:
        workspace_root: 工作区根目录；为空或纯空白时视为无 workspace，直接返回默认规则，
            不会退化成「在进程当前目录创建规则文件」。

    返回:
        解析后的规则对象。文件缺失且创建成功时，文件已写入默认规则，返回值即默认规则。

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
    return parse_ignore_rules(_limit_rules(_read_ignore_text(path), path), workspace_root=root)


def _limit_rules(text: str, path: Path) -> str:
    """把规则文本裁到 ``_MAX_RULES`` 行以内，超限时写 WARNING（带文件路径便于定位）。

    参数:
        text: 规则文件全文（可能已被 ``_MAX_FILE_BYTES`` 截断）。
        path: 规则文件路径，仅用于日志定位。

    返回:
        最多 ``_MAX_RULES`` 行的文本；未超限时原样返回。

    异常:
        无。

    副作用:
        超限时写 WARNING 日志 ``file_ignore_rules_truncated``。
    """

    lines = text.splitlines()
    if len(lines) <= _MAX_RULES:
        return text
    log.warning(
        "file_ignore_rules_truncated",
        extra={
            "msg": ".fileignore 行数超过上限，多余行已忽略",
            "data": {"path": str(path), "limit": _MAX_RULES, "lines": len(lines)},
        },
    )
    return "\n".join(lines[:_MAX_RULES])


def _read_ignore_text(path: Path) -> str:
    """读取规则文件文本；文件不存在时创建默认文件，失败时返回默认文本。

    不用 ``app.utils.file_utils.read_text_file`` 收口：本函数需要按上限读取并在读取失败时降级，
    而该收口是「整文件 UTF-8 读取」的薄包装，无法表达这两点。

    参数:
        path: 规则文件路径。

    返回:
        文件全文；文件缺失且创建成功（或被并发方创建）时为该文件内容，创建失败时为默认文本。

    异常:
        无：路径非法（如含 NUL 字节）、读写失败或解码失败一律降级为默认文本并写 WARNING 日志。

    副作用:
        可能创建父目录与规则文件；并发创建命中的 ``FileExistsError`` 视为「他人已建」。
    """

    try:
        return _read_limited(path)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        _log_read_failure(path, exc)
        return render_default_content()

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
            exc_info=True,
        )
    return content


def _read_limited(path: Path) -> str:
    """按 ``_MAX_FILE_BYTES`` 上限读取规则文件。

    参数:
        path: 规则文件路径。

    返回:
        文件文本；超过上限时只返回前 ``_MAX_FILE_BYTES`` 个字符。

    异常:
        OSError: 文件不存在或无权限等 I/O 错误（由调用方降级）。
        ValueError: 路径非法（如含 NUL 字节）或 UTF-8 解码失败（``UnicodeDecodeError``）。

    副作用:
        无（只读文件）。
    """

    with path.open("r", encoding="utf-8") as handle:
        return handle.read(_MAX_FILE_BYTES)


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
