"""list_directory 工具实现。

本模块只承载 list_directory 这一个工具。列出项目内目录条目（名称 / 类型 /
路径），路径安全委托 ``security.ProjectPathResolver``。

设计边界：
- 路径安全委托 ``security.ProjectPathResolver``，不内联路径规则。
- 只列目录，不读文件内容、不写文件。
"""

import fnmatch
import os
from pathlib import Path
from typing import Any

from app.core.tools.display.filesystem_display import build_directory_display_data
from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import blocked_device_reason, tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.list_directory_args import ListDirectoryArgs


class ListDirectoryTool(HandlerBase):
    """列出项目内目录条目的工具类（无状态）。

    返回:
        ``ListDirectoryTool`` 实例。

    异常:
        初始化阶段不主动抛出业务异常。

    副作用:
        不持有文件系统状态，不读取、不写入文件；项目根在执行时从
        ``execution_context.workspace_root`` 取得。
    """

    name = "list_directory"
    description = (
        "List a directory's entries: name, type (file|dir|link), path. Read-only. "
        'Pass path="." for the workspace root; relative paths resolve against the root. '
        "Hidden entries are skipped unless include_hidden is true; use include_globs to show "
        "only entries whose name matches given patterns."
    )
    permission = "file_search"
    args_model = ListDirectoryArgs
    timeout_seconds = 15.0
    risk_level = "low"

    def __init__(self) -> None:
        """初始化 list_directory 工具实例（无状态）。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            不保存任何状态、不执行文件系统操作；项目根在执行时从
            ``execution_context.workspace_root`` 取得。
        """

    def execute(
        self,
        path: str,
        execution_context: ToolExecutionContext,
        offset: int = 0,
        limit: int = 200,
        include_hidden: bool = False,
        include_globs: list[str] | None = None,
    ) -> ToolObservation:
        """列出项目内目录条目，返回结构化观察结果。

        参数:
            path: 待列举的目录路径；相对路径以 workspace 根为基准，也接受项目根外的
                绝对路径（只读不受 workspace 边界限制）。
            offset: 跳过前 N 个排序后的条目。
            limit: 单页最多返回的条目数。
            include_hidden: 为 true 时一并列出 dot 条目（名称以 ``.`` 开头），默认跳过
                以保持目录清单紧凑。
            include_globs: 正向白名单 glob 模式列表；仅「条目名」匹配其中任一模式的条目
                会被展示，其余忽略。``None``/空列表表示不过滤（展示全部，仍受
                ``include_hidden`` 控制）。``os.scandir`` 仅列直接子项，故 glob 只按条目名
                匹配（带目录前缀的模式无意义）。与 ``include_hidden`` 独立叠加
                （先按可见性过滤，再按本参数白名单筛选）。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；
                由执行链在执行期强制注入，handler 契约必须接受此 kwarg。
                本工具只读，但解析相对路径仍需工作区根，故消费其 ``workspace_root``。

        返回:
            ``ToolObservation``；成功时 content 为模型继续工作所需的紧凑条目表；
            失败时 ``error`` 描述事实，``reason`` 提供下一步动作。

        异常:
            不主动向上抛出；路径解析失败、目录不存在、非目录、以及列举时的
            ``OSError``（如 TOCTOU 竞态下目录被删除或权限撤销）均归一化为结构化观察。

        副作用:
            只读目录结构，不修改文件系统。
        """
        # 模型常把根目录误解为空字符串；这里安全归一化为 "."，既兼容 LLM 输入，
        # 又不影响安全校验（PathResolver 随后仍会解析 "."）。
        if path == "":
            path = ""
        root = execution_context.workspace_root
        resolver = PathResolver(root)
        device_error = resolver.blocked_device_reason(path)
        if device_error:
            return tool_error(
                self.name,
                device_error,
                reason=blocked_device_reason("listed"),
                permission=self.permission,
            )
        resolved, error = resolver.resolve_without_boundary(path)
        if resolved is None:
            return tool_error(
                self.name,
                f"could not list the directory: {error}",
                reason="provide a valid directory path.",
                retryable=True,
                permission=self.permission,
            )
        device_error = resolver.blocked_device_reason(path, resolved)
        if device_error:
            return tool_error(
                self.name,
                device_error,
                reason=blocked_device_reason("listed"),
                permission=self.permission,
            )
        if not resolved.exists():
            return tool_error(
                self.name,
                f"could not list the directory: no such path at '{resolved}'",
                reason="provide the current path of an existing directory.",
                retryable=True,
                permission=self.permission,
            )
        if not resolved.is_dir():
            return tool_error(
                self.name,
                f"could not list '{resolved}': it is a file, not a directory",
                reason="provide a directory path, or use read_file for a file.",
                retryable=True,
                permission=self.permission,
            )

        children: list[os.DirEntry[str]] = []
        try:
            with os.scandir(resolved) as scan:
                raw_entries = [
                    entry for entry in scan if include_hidden or not entry.name.startswith(".")
                ]
                if include_globs:
                    # os.scandir 只列直接子项，条目相对列举根的 rel 恒等于 entry.name，
                    # 故 glob 仅按「条目名」匹配（带目录前缀的模式在此单层列举下无意义）。
                    # 正向白名单：只保留匹配任一模式的条目，其余忽略。
                    raw_entries = [
                        entry
                        for entry in raw_entries
                        if any(fnmatch.fnmatch(entry.name, g) for g in include_globs)
                    ]
                children = sorted(raw_entries, key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError as exc:
            # exists()/is_dir() 检查与 scandir 之间存在 TOCTOU 窗口：目录可能在检查后被
            # 并发删除、移动或撤销权限，导致 scandir 抛 OSError/FileNotFoundError。
            # 必须归一化为结构化错误而非让异常逃逸到工具调度层，否则模型会收到未处理的
            # raw 异常且可能中断 turn 流式。
            return tool_error(
                self.name,
                f"could not list '{resolved}': {exc}",
                reason=(
                    "check that the directory remains available and readable, then call "
                    "list_directory again."
                ),
                retryable=True,
                permission=self.permission,
            )
        page = children[offset : offset + limit]
        entries: list[str] = []
        entry_dicts: list[dict[str, Any]] = []
        for entry in page:
            # 符号链接优先判为 "link"，否则指向目录的链接会被 is_dir() 误判为 file，
            # 误导模型对链接目录的理解。
            entry_type = "link" if entry.is_symlink() else "dir" if entry.is_dir() else "file"
            entry_path = self._normalize_posix_path((resolved / entry.name).as_posix())
            entries.append(f"{entry_type:4s}  {entry.name}  ({entry_path})")
            entry_dicts.append(
                {
                    "name": entry.name,
                    "type": entry_type,
                    # 条目的真实绝对路径（含文件名），用列举根 + 条目名词法拼接，
                    # 不解析 symlink，保持用户可见路径；避免相对路径/越界绝对路径
                    # 在前端拼接后续工具调用时失真。
                    "path": entry_path,
                }
            )
        content = "\n".join(entries) if entries else "(empty directory)"
        # offset 越界（目录非空但当前页为空）单独提示，避免模型把「越界」误判为「空目录」，
        # 否则幻觉出的大 offset 会得到与真实空目录无法区分的静默结果。
        if not entries and children and offset >= len(children):
            content = (
                f"(no entries at offset={offset}; directory has {len(children)} "
                f"entries, valid offset range is 0..{len(children) - 1})"
            )
        next_offset = offset + len(page) if offset + len(page) < len(children) else None
        if next_offset is not None:
            content += (
                f"\n\n[Hint: Results truncated ({len(children)} total). "
                f"Use offset={next_offset} to continue.]"
            )

        return tool_success(
            tool_name=self.name,
            content=content,
            permission=self.permission,
            display_data=build_directory_display_data(
                path=path,
                entries=entry_dicts,
                offset=offset,
                limit=limit,
                total_entries=len(children),
                next_offset=next_offset,
            ),
        )

    def _normalize_posix_path(self, path: str) -> str:
        """把任意路径字符串归一化为 posix 风格（正斜杠）绝对/相对路径。

        仅做 ``Path(path).as_posix()`` 归一化，不推导父目录、不解析符号链接。
        调用的「条目真实路径」已由调用方完成词法拼接，此处只负责统一分隔符风格，
        供前端稳定拼接后续工具调用。

        参数:
            path: 待归一化的路径字符串（已含完整绝对或相对路径）。

        返回:
            posix 风格路径字符串；空串返回 ``"."``。

        异常:
            无。

        副作用:
            无。
        """
        if not path:
            return "."
        normalized = Path(path)
        return normalized.as_posix()

    def to_definition(self) -> ToolDefinition:
        """把工具实例转换成 ``ToolDefinition``。

        参数:
            无。

        返回:
            可直接注册到 ``ToolRegistry`` 的工具定义（含 display 展示元数据）。

        异常:
            无。

        副作用:
            无。
        """

        return ToolDefinition(
            name=self.name,
            description=self.description,
            permission=self.permission,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            resource_keys=("filesystem",),
            display=ToolDisplayHints(
                verb="查看目录",
                icon="eye",
                expandable=True,
                expand_layout="list",
                show_result=False,
            ),
        )


def build_list_directory_definition() -> ToolDefinition:
    """构造绑定到指定项目根目录的 list_directory 工具定义。

    参数:
        无。

    返回:
        ``ToolDefinition``，供 ``ToolRegistry`` 注册。

    异常:
        无。

    副作用:
        创建 ``ListDirectoryTool`` 实例和定义对象，不执行文件系统操作。
    """

    return ListDirectoryTool().to_definition_if_avaliable()
