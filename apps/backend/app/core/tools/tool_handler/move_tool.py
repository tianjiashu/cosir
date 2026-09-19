"""把工作区内唯一一个已存在的 UTF-8 文本文件移动到新路径。

本模块只承载 ``move_file`` 工具：源与目标路径校验、移动前状态复检与观察归一化。文件创建、
内容修改与删除分别由 ``write_file``、``apply_patch``、``delete_file`` 负责。

设计边界：移动不改变内容，展示载荷只描述「源路径 → 目标路径」。被移动文件的内容既不进入
UI 展示通道也不进入模型通道，因此本模块只对源文件做**有界采样**的文本性校验
（``ensure_utf8_text_file``），不整读文件。
"""

import os

from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.tools.display.file_change_display import build_file_change_display_data
from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_cancelled import tool_cancelled
from app.core.tools.tool_execute.tool_error import os_error_message, tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.patch_write.patch_diff import FileDiffResult
from app.core.tools.tool_handler.security.file_operation_paths import (
    ensure_utf8_text_file,
    resolve_workspace_relative_path,
)
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.move_file_args import MoveFileArgs

MOVE_FILE_DESCRIPTION = (
    "Move one existing UTF-8 text file to a new path inside the active workspace. Directories "
    "are not supported. The source must be a regular file, the destination must not already "
    "exist, and both paths must resolve inside the workspace. The destination parent directory "
    "must already exist. This tool moves a file without editing its contents; use apply_patch "
    "for content changes."
)


def _move_without_overwriting(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
    """移动普通文件，且不覆盖并发创建出来的目标。

    Windows 的 ``os.rename`` 在目标已存在时本来就会失败；POSIX 的 ``rename`` 则会直接覆盖，
    因此改用文件系统提供的「原子不覆盖硬链接」操作，再删除源文件。两个路径必须位于同一文件
    系统（与这里的 ``os.rename`` 要求一致）。

    参数:
        source: 源文件路径。
        destination: 目标文件路径；必须不存在（调用方已完成该检查）。

    返回:
        无。

    异常:
        OSError: 目标已存在、移动无法完成，或建立链接后源文件无法删除。删除源文件失败时会先
            尝试移除仍与源文件同 inode 的目标链接，再向上抛出。

    副作用:
        改写目录项：建立目标链接并删除源路径；失败补偿路径可能删除刚建立的目标链接。
    """

    if os.name == "nt":
        os.rename(source, destination)
        return

    os.link(source, destination)
    try:
        os.unlink(source)
    except BaseException:
        try:
            if os.path.samefile(source, destination):
                os.unlink(destination)
        except OSError:
            pass
        raise


class MoveTool(HandlerBase):
    """移动一个工作区文件，且不修改其内容。

    职责边界：
    - 负责：把一次 ``move_file`` 调用编排为「源路径解析 → 源形态有界校验 → 目标路径解析 →
      目标前置检查 → 取消检查 → 移动前复检 → 落盘移动 → 观察归一化」。
    - 不负责：路径边界与设备路径判定（``resolve_workspace_relative_path`` /
      ``PathResolver``）、源文本性采样（``ensure_utf8_text_file``）、移动前守卫
      展示载荷构造（``build_file_change_display_data``）。
    """

    name = "move_file"
    description = MOVE_FILE_DESCRIPTION
    permission = "file_write"
    args_model = MoveFileArgs
    timeout_seconds = 15.0
    risk_level = "medium"

    def execute(
        self,
        execution_context: ToolExecutionContext,
        source_path: str,
        destination_path: str,
    ) -> ToolObservation:
        """校验源与目标，并执行文件移动。

        参数:
            execution_context: 本次执行的运行时边界；``workspace_root`` 决定两个路径的边界，
                ``run_id`` 用于移动前的取消检查。
            source_path: 工作区相对源路径，必须指向一个已存在的普通 UTF-8 文本文件。
            destination_path: 工作区相对目标路径；必须不存在、父目录已存在，且与源路径不同。

        返回:
            成功为 ``status="success"``，携带 ``moved`` 状态的 ``file-changes`` 展示载荷
            （源路径 → 目标路径，不含文件内容）；路径非法、源不可读、目标已存在、目标父目录
            缺失、移动前路径被改动均为 ``status="error"``；移动前检出取消为
            ``status="cancelled"``。

        异常:
            无：``OSError`` 与路径变更都归一化为 :func:`tool_error`。

        副作用:
            把源文件移动到目标路径（不覆盖语义）；移动前复检两侧解析结果未被并发改动。
            源内容只做有界文本性采样，不整读文件。
        """

        resolver = PathResolver(execution_context.workspace_root)
        source, error = resolve_workspace_relative_path(resolver, source_path, label="source_path")
        if source is None:
            return self._error(
                error, "provide a workspace-relative path to one existing UTF-8 text file."
            )
        source_text_error = ensure_utf8_text_file(source, label="move source")
        if source_text_error:
            return self._error(
                source_text_error, "the move source must be one existing regular UTF-8 text file."
            )
        destination, error = resolve_workspace_relative_path(
            resolver,
            destination_path,
            label="destination_path",
        )
        if destination is None:
            return self._error(
                error, "provide a workspace-relative destination path inside the workspace."
            )
        if source == destination:
            return self._error(
                "source and destination resolve to the same file",
                "choose a different destination path.",
            )
        if os.path.lexists(destination):
            return self._error(
                "move destination already exists",
                "choose a destination path that does not already exist.",
            )
        if not destination.parent.is_dir():
            return self._error(
                "move destination parent directory does not exist",
                "choose a destination whose parent directory already exists.",
            )
        if cancellation_registry.is_cancelled(execution_context.run_id):
            return tool_cancelled(tool_name=self.name, permission=self.permission)
        try:
            current_source, source_error = resolver.resolve_within_workspace(
                source_path.replace("\\", "/")
            )
            current_destination, destination_error = resolver.resolve_within_workspace(
                destination_path.replace("\\", "/")
            )
            if (
                current_source is None
                or source_error
                or current_source != source
                or current_destination is None
                or destination_error
                or current_destination != destination
                or os.path.lexists(destination)
                or not destination.parent.is_dir()
            ):
                raise RuntimeError("move paths changed before mutation")
            _move_without_overwriting(source, destination)
        except OSError as exc:
            return tool_error(
                tool_name=self.name,
                error=os_error_message(exc, "move the file"),
                reason="check the current source and destination states before trying again.",
                retryable=True,
                permission=self.permission,
            )
        except (RuntimeError, ValueError) as exc:
            return self._error(
                str(exc), "re-read both paths and confirm the source and destination are unchanged."
            )

        root = execution_context.workspace_root.resolve()
        source_relative = source.relative_to(root).as_posix()
        destination_relative = destination.relative_to(root).as_posix()
        display_data = build_file_change_display_data(
            [
                # moved 状态只投影 rename 元数据：format_git_diff 与 build_diff_stats 都不消费
                # before/after，因此这里不携带内容，也不需要读取源文件正文。
                FileDiffResult(
                    path=source_relative,
                    new_path=destination_relative,
                    status="moved",
                    before="",
                    after="",
                )
            ]
        )
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=None,
            display_data=display_data,
        )

    def _error(self, error: str, reason: str) -> ToolObservation:
        """构造确定性的失败观察，并附一句简短的修正指令。

        参数:
            error: 面向模型的**英文**错误事实（发生了什么）。
            reason: 面向模型的**英文**修正建议（下一步怎么做）。

        返回:
            ``status="error"``、``retryable=False`` 的 :class:`ToolObservation`；本方法服务的
            都是确定性失败，修正参数或当前状态前重试没有意义。

        异常:
            无。

        副作用:
            无（只构造观察，不改动文件系统）。
        """

        return tool_error(
            tool_name=self.name,
            error=error,
            reason=reason,
            retryable=False,
            permission=self.permission,
        )

    def to_definition(self) -> ToolDefinition:
        """返回该工具的注册表定义与静态 diff 展示声明。

        返回:
            以 ``self.execute`` 为 handler、携带 ``diff`` 展开布局展示声明的
            :class:`ToolDefinition`。

        异常:
            无。

        副作用:
            无（不访问文件系统）。
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
                verb="移动文件",
                icon="file-symlink",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )


def build_move_file_definition() -> ToolDefinition:
    """构造 ``move_file`` 的注册表定义。

    返回:
        由 :class:`MoveTool` 产出的 :class:`ToolDefinition`。

    异常:
        无。

    副作用:
        无。
    """

    return MoveTool().to_definition()
