"""删除工作区内唯一一个已存在的普通文件。

本模块只承载 ``delete_file`` 工具：路径校验、删除前状态复检与观察归一化。目录删除不在此模块，
文件创建、内容修改与移动分别由 ``write_file``、``apply_patch``、``move_file`` 负责。

设计边界：删除只关心「目标是谁」，不关心「内容是什么」。删除结果只通过最小的
``file-changes`` 展示载荷报告路径和状态，不读取文件内容；二进制与图片文件同样允许删除，
删除代价不随文件体积增长。
"""

from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.tools.display.file_change_display import build_file_delete_display_data
from app.core.tools.schemas import (
    TOOL_DELETE_FILE,
    TOOL_GROUP_FILE_EDIT,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_cancelled import tool_cancelled
from app.core.tools.tool_execute.tool_error import os_error_message, tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.security.file_operation_paths import (
    resolve_workspace_relative_path,
)
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.delete_file_args import DeleteFileArgs

DELETE_FILE_DESCRIPTION = (
    "Delete exactly one existing file inside the active workspace. Directories are not supported. "
    "The path must resolve to a regular file within the workspace; binary and image files are "
    "allowed, and the file's contents are never read or returned. Use this tool for file "
    "deletion; do not encode deletion in apply_patch."
)


class DeleteTool(HandlerBase):
    """删除一个工作区文件，并返回标准的文件变更展示载荷。

    职责边界：
    - 负责：把一次 ``delete_file`` 调用编排为「路径解析 → 目标形态校验 → 取消检查 →
      删除前状态复检 → 落盘删除 → 观察归一化」。
    - 不负责：路径边界与设备路径判定（``resolve_workspace_relative_path`` /
      ``PathResolver``）、展示载荷构造（``build_file_delete_display_data``）。
    """

    name = TOOL_DELETE_FILE
    description = DELETE_FILE_DESCRIPTION
    permission = "file_write"
    args_model = DeleteFileArgs
    timeout_seconds = 15.0
    risk_level = "medium"
    group = TOOL_GROUP_FILE_EDIT


    def execute(self, execution_context: ToolExecutionContext, path: str) -> ToolObservation:
        """校验目标并删除一个已存在的普通文件。

        参数:
            execution_context: 本次执行的运行时边界；``workspace_root`` 决定路径边界，
                ``run_id`` 用于删除前的取消检查。
            path: 工作区相对路径，必须指向一个已存在的普通文件（不支持目录；二进制文件允许）。

        返回:
            成功为 ``status="success"``，携带只有目标路径与 ``deleted`` 状态的
            ``file-changes`` 展示载荷（不含被删内容）；路径非法、目标不是普通文件、删除前目标
            被改动均为 ``status="error"``；删除前检出取消为 ``status="cancelled"``。

        异常:
            无：``OSError`` 与目标变更都归一化为 :func:`tool_error`。

        副作用:
            即时删除目标文件；删除前复检解析结果仍指向同一路径，避免把并发换掉的实体删掉。
            不读取目标内容，因此不受文件体积影响。
        """

        resolver = PathResolver(execution_context.workspace_root)
        resolved, error = resolve_workspace_relative_path(resolver, path, label="path")
        if resolved is None:
            return tool_error(
                tool_name=self.name,
                error=error,
                reason=(
                    "provide a workspace-relative path to an existing file; "
                    "directories are not supported."
                ),
                retryable=False,
                permission=self.permission,
            )
        if not resolved.is_file():
            return tool_error(
                tool_name=self.name,
                error="delete target is not an existing regular file",
                reason="provide a path to one existing regular file inside the workspace.",
                retryable=False,
                permission=self.permission,
            )
        if cancellation_registry.is_cancelled(execution_context.run_id):
            return tool_cancelled(tool_name=self.name, permission=self.permission)
        try:
            current, current_error = resolver.resolve_within_workspace(path.replace("\\", "/"))
            if current is None or current_error or current != resolved or not resolved.is_file():
                raise RuntimeError("delete target changed before mutation")
            resolved.unlink()
        except OSError as exc:
            return tool_error(
                tool_name=self.name,
                error=os_error_message(exc, "delete the target file"),
                reason="check the file's current state and permissions before trying again.",
                retryable=True,
                permission=self.permission,
            )
        except (RuntimeError, ValueError) as exc:
            return tool_error(
                tool_name=self.name,
                error=str(exc),
                reason=(
                    "re-read the target path and confirm it is still the intended workspace file."
                ),
                retryable=False,
                permission=self.permission,
            )

        relative = resolved.relative_to(execution_context.workspace_root.resolve()).as_posix()
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=None,
            display_data=build_file_delete_display_data(relative),
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
            group=self.group,
            description=self.description,
            permission=self.permission,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            resource_keys=("filesystem",),
            display=ToolDisplayHints(
                verb="删除文件",
                icon="file-x",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )


def build_delete_file_definition() -> ToolDefinition:
    """构造 ``delete_file`` 的注册表定义。

    返回:
        由 :class:`DeleteTool` 产出的 :class:`ToolDefinition`。

    异常:
        无。

    副作用:
        无。
    """

    return DeleteTool().to_definition_if_avaliable()
