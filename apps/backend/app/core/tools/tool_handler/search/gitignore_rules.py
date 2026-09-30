"""gitignore 风格（``pathspec`` gitwildmatch）忽略规则的通用解析与匹配。

单一职责：把 gitignore 风格文本解析为 :class:`GitignoreMatcher`，供搜索引擎在遍历时跳过被忽略的
文件与目录（含 ``*.log``、``build/``、``**/x``、``!keep`` 否定等完整 git 语义）。

这是 ``.gitignore`` 与 ``.fileignore`` 共用的基础能力——两个文件都采用标准 gitignore 语义，仅
文件路径与相对根不同。不负责目录遍历（见 ``file_walker``）。

v1 仅支持 workspace 根目录的 ``.gitignore``；子目录嵌套 ``.gitignore`` 暂不支持（后续迭代）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pathspec

from app.config.logging.logger import log

GITIGNORE_FILE_NAME: str = ".gitignore"
"""workspace 根目录下的 git 忽略文件名。"""


def gitignore_path(workspace_root: str | Path) -> Path:
    """返回 workspace 根的 ``.gitignore`` 路径（不校验存在性）。"""

    return Path(workspace_root) / GITIGNORE_FILE_NAME


def build_gitwildmatch_spec(lines: list[str]) -> pathspec.PathSpec:
    """把 gitignore 风格文本行（已过滤注释/空行）解析为 ``pathspec.PathSpec``。

    参数:
        lines: 规则行（调用方负责去除注释与空行）。

    返回:
        对应的 ``pathspec.PathSpec``；空列表返回无规则的 spec。

    异常:
        无：pathspec 对绝大多数输入宽容，极端非法输入由调用方兜底。

    副作用:
        无（纯解析）。
    """

    return pathspec.PathSpec.from_lines("gitwildmatch", lines)


@dataclass(frozen=True)
class GitignoreMatcher:
    """基于 ``pathspec`` 的 git 忽略匹配器（标准 gitwildmatch 语义）。

    ``match_dir`` 用「相对路径 + '/'」判定目录（吞掉 ``build/`` 这类目录模式），``match_file`` 用
    相对路径判定文件；相对根之外的路径退化为不命中（仅 workspace 内路径参与匹配）。

    当 ``workspace_root`` 为 ``None`` 时，路径直接以其 POSIX 形式参与匹配（用于无 workspace 上下文的
    兜底默认规则：此时通配模式按路径各段匹配）。
    """

    workspace_root: Path | None
    spec: pathspec.PathSpec

    def _relative_posix(self, path: Path) -> str | None:
        """把路径转为相对 workspace 根的 POSIX 路径；根未知时退化为路径自身的 POSIX 形式。"""

        if self.workspace_root is None:
            return path.as_posix()
        try:
            return path.relative_to(self.workspace_root).as_posix()
        except ValueError:
            return None

    def match_dir(self, directory: Path) -> bool:
        """目录是否命中 git 忽略（含其整棵子树）。"""

        rel = self._relative_posix(directory)
        return rel is not None and self.spec.match_file(rel + "/")

    def match_file(self, file: Path) -> bool:
        """文件是否命中 git 忽略。"""

        rel = self._relative_posix(file)
        return rel is not None and self.spec.match_file(rel)


def _valid_lines(text: str) -> list[str]:
    """过滤出有效规则行（去除首尾空白、注释与空行）。"""

    lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            lines.append(stripped)
    return lines


def load_gitignore_style(
    path: Path, *, workspace_root: Path | None
) -> GitignoreMatcher | None:
    """读取任意 gitignore 风格文件并解析为 :class:`GitignoreMatcher`。

    供 ``.gitignore`` 与 ``.fileignore`` 复用以共享标准 gitignore 语义。文件缺失或读取失败时返回
    None（不参与忽略）；空文件或过滤后没有任何有效规则时也返回 None。

    参数:
        path: 规则文件路径（读取用）。
        workspace_root: 相对路径匹配的基准根；``.gitignore``/``.fileignore`` 均相对 workspace 根。

    返回:
        解析成功的匹配器；文件不存在、为空或读取失败时为 None。

    异常:
        无：任意读取/解析异常一律降级为 None 并写 WARNING 日志，绝不阻断搜索。

    副作用:
        只读文件系统；不创建或修改任何文件。
    """

    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning(
            "gitignore_read_failed",
            extra={
                "msg": f"{path.name} 读取失败，本次不纳入其忽略规则",
                "data": {"path": str(path), "error": str(exc), "error_type": type(exc).__name__},
            },
        )
        return None
    lines = _valid_lines(text)
    if not lines:
        return None
    try:
        spec = build_gitwildmatch_spec(lines)
    except Exception as exc:  # pathspec 对绝大多数输入宽容，此处仅兜底极端情形
        log.warning(
            "gitignore_parse_failed",
            extra={
                "msg": f"{path.name} 解析失败，本次不纳入其忽略规则",
                "data": {"path": str(path), "error": str(exc), "error_type": type(exc).__name__},
            },
        )
        return None
    return GitignoreMatcher(workspace_root=workspace_root, spec=spec)


def load_gitignore_rules(workspace_root: str | Path) -> GitignoreMatcher | None:
    """读取 workspace 根的 ``.gitignore``；文件缺失或读取失败时返回 None（不参与忽略）。

    参数:
        workspace_root: 工作区根目录；为空时直接返回 None。

    返回:
        :class:`GitignoreMatcher`；文件不存在、为空或读取失败时为 None。

    异常:
        无。

    副作用:
        只读文件系统。
    """

    root_text = str(workspace_root).strip()
    if not root_text:
        return None
    return load_gitignore_style(gitignore_path(root_text), workspace_root=Path(root_text))
