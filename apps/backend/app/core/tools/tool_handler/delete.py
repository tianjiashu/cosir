"""delete 工具实现。

本模块只承载 delete 这一个工具。删除项目内文件或目录，路径安全委托
``security.ProjectPathResolver``；删目录默认仅删空目录，删树需显式 ``recursive=True``。

设计边界：
- 路径安全委托 ``security.ProjectPathResolver``，不内联路径规则。
- 单工具覆盖文件与目录：运行时按目标类型分支，不暴露两个平行工具。
- 默认安全：``recursive=False`` 仅删空目录，非空目录返回 not_empty 错误。
- 拒绝删除项目根自身（root_self_delete），避免误删整棵工作区。
- 文件（含指向 workspace 内的符号链接）只删链接本身；目录走 rmdir / rmtree。
"""

import errno
import shutil

from app.config.logging.logger import log
from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import (
    blocked_device_reason,
    os_error_message,
    tool_error,
)
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.core.tools.tool_handler.security.windows_reparse_point import (
    is_windows_directory_reparse_point,
)
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.delete_args import DeleteArgs


class DeleteTool(HandlerBase):
    """删除项目内文件或目录的工具类（workspace 作用域，不审批）。

    单工具覆盖文件与目录：运行时按目标类型分支，不暴露 delete_file /
    delete_directory 两个平行工具。路径安全委托 ``security.ProjectPathResolver``；
    删目录默认仅删空目录，删树需显式 ``recursive=True``。

    参数:
        无。

    返回:
        ``DeleteTool`` 实例。

    异常:
        初始化阶段不主动抛出业务异常。

    副作用:
        仅保存工具元数据；不读取、不写入文件。
    """

    name = "delete"
    description = (
        "Delete a file or directory inside the project root. Directories only delete when "
        "empty by default; pass recursive=true to delete a non-empty directory tree. "
        "Refuses the project root and paths outside the project root."
    )
    permission = "file_delete"
    args_model = DeleteArgs
    timeout_seconds = 30.0
    risk_level = "high"

    def __init__(self) -> None:
        """初始化 delete 工具实例。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """

    def execute(
        self,
        path: str,
        recursive: bool = False,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """删除项目内文件或目录，返回结构化观察结果。

        参数:
            path: 待删除的文件或目录路径（相对项目根）。
            recursive: 是否递归删除目录树；``False`` 时仅删空目录，非空目录返回
                not_empty 错误。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；
                由执行链在执行期强制注入，handler 契约必须接受此 kwarg。
                破坏性操作以其 ``workspace_root`` 作为路径 containment 的唯一事实源。

        返回:
            ``ToolObservation``；成功时 content 为删除确认（删目录且 recursive 时
            标注 "(recursive)"），data 含 ``path`` / ``type``（"file" 或 "dir"）/
            ``recursive``；失败时 status 为 error，``error``/``reason`` 提供面向模型的
            富文本诊断（``error``=发生了什么、``reason``=为什么失败+如何修正+是否重试）。

        异常:
            不主动向上抛出；路径/删除错误归一化为结构化观察。

        副作用:
            成功时删除目标文件（或符号链接本身）或目录（recursive=True 时连同子树）。
        """
        assert execution_context is not None, "delete requires a workspace execution context"
        root = execution_context.workspace_root
        root_resolved = root.resolve()
        resolver = PathResolver(root)
        device_error = resolver.blocked_device_reason(path)
        if device_error:
            return tool_error(
                self.name,
                device_error,
                reason=blocked_device_reason("deleted"),
                permission=self.permission,
            )
        entry, error = resolver.resolve_entry_within_workspace(path)
        if entry is None:
            return tool_error(
                self.name,
                f"could not delete the target: {error}",
                reason=(
                    "the path escapes the project workspace and cannot be deleted. The "
                    "resolver rejects paths that point outside the workspace root for "
                    "safety. Pass a path inside the project (relative to the workspace "
                    "root, or an absolute path under it); the same out-of-bounds path will "
                    "always be rejected."
                ),
                permission=self.permission,
            )
        device_error = resolver.blocked_device_reason(path, entry)
        if device_error:
            return tool_error(
                self.name,
                device_error,
                reason=blocked_device_reason("deleted"),
                permission=self.permission,
            )
        if entry == root_resolved:
            return tool_error(
                self.name,
                "refusing to delete the project root",
                reason=(
                    "the path resolves to the project root itself; deleting the entire "
                    "workspace is refused to prevent catastrophic data loss. Pass a "
                    "specific file or subdirectory inside the project; the project root "
                    "will always be rejected."
                ),
                permission=self.permission,
            )
        is_symlink = entry.is_symlink()
        is_junction = is_windows_directory_reparse_point(entry)
        if is_symlink or is_junction:
            try:
                current_entry, current_error = resolver.resolve_entry_within_workspace(path)
                current_is_link = current_entry is not None and (
                    current_entry.is_symlink() or is_windows_directory_reparse_point(current_entry)
                )
                if current_entry != entry or current_error or not current_is_link:
                    raise OSError(errno.EAGAIN, "delete target changed before unlink")
                if is_junction:
                    entry.rmdir()
                else:
                    entry.unlink()
            except OSError as exc:
                return tool_error(
                    self.name,
                    os_error_message(exc, "delete the target"),
                    reason=(
                        "the link entry could not be removed, usually because it is "
                        "locked by another process, the current user lacks delete "
                        "permission, or the target changed concurrently. Close the "
                        "program holding it or adjust permissions, then retry the same "
                        "delete."
                    ),
                    retryable=True,
                    permission=self.permission,
                )
            return tool_success(
                tool_name=self.name,
                content=f"Deleted link entry: {entry}",
                permission=self.permission,
                data={
                    "kind": "delete-result",
                    "path": path,
                    "target_type": "link",
                    "recursive": False,
                },
            )
        if not entry.exists():
            return tool_error(
                self.name,
                f"could not delete the target: no such file or directory at '{entry}'",
                reason=(
                    "the target does not exist at the given path. Check for a typo, "
                    "confirm the file or directory was not moved or deleted, or pass an "
                    "absolute path. The same non-existent path will always fail, so retry "
                    "only after the target exists or the path is corrected."
                ),
                permission=self.permission,
            )
        resolved, error = resolver.resolve_within_workspace(path)
        if resolved is None:
            return tool_error(
                self.name,
                f"could not delete the target: {error}",
                reason=(
                    "the path escapes the project workspace and cannot be deleted. The "
                    "resolver rejects paths that point outside the workspace root for "
                    "safety. Pass a path inside the project (relative to the workspace "
                    "root, or an absolute path under it); the same out-of-bounds path will "
                    "always be rejected."
                ),
                permission=self.permission,
            )
        if resolved == root_resolved:
            return tool_error(
                self.name,
                "refusing to delete the project root",
                reason=(
                    "the path resolves to the project root itself; deleting the entire "
                    "workspace is refused to prevent catastrophic data loss. Pass a "
                    "specific file or subdirectory inside the project; the project root "
                    "will always be rejected."
                ),
                permission=self.permission,
            )
        if resolved.is_dir():
            try:
                current_resolved, current_error = resolver.resolve_within_workspace(path)
                if (
                    current_resolved != resolved
                    or current_error
                    or current_resolved == root_resolved
                ):
                    raise OSError(errno.EAGAIN, "delete target changed before directory removal")
                if recursive:
                    shutil.rmtree(resolved)
                else:
                    resolved.rmdir()
            except OSError as exc:
                if not recursive and exc.errno == errno.ENOTEMPTY:
                    return tool_error(
                        self.name,
                        f"directory not empty: {resolved}; pass recursive=true to delete",
                        reason=(
                            "the directory is not empty, so it cannot be deleted without "
                            "recursive=true. Either pass recursive=true to delete the whole "
                            "tree, or first remove its contents; the same non-empty "
                            "directory will always fail unless recursive=true is set."
                        ),
                        permission=self.permission,
                    )
                return tool_error(
                    self.name,
                    os_error_message(exc, "delete the target"),
                    reason=(
                        "the directory could not be removed, usually because it is locked "
                        "by another process, the current user lacks delete permission, or "
                        "its contents changed concurrently. Close the program holding it "
                        "or adjust permissions, then retry the same delete."
                    ),
                    retryable=True,
                    permission=self.permission,
                )
            return tool_success(
                tool_name=self.name,
                permission=self.permission,
                content=f"Deleted directory: {resolved}" + (" (recursive)" if recursive else ""),
                data={
                    "kind": "delete-result",
                    "path": path,
                    "target_type": "directory",
                    "recursive": recursive,
                },
            )
        try:
            current_resolved, current_error = resolver.resolve_within_workspace(path)
            if current_resolved != resolved or current_error:
                raise OSError(errno.EAGAIN, "delete target changed before unlink")
            # 删除前读取全文，供文件快照采集（Turn 回退可据此重建文件）。
            # 读取失败不阻断删除：降级为 before 空串并保留 warning 日志。
            before_content = ""
            try:
                # 用 bytes 解码而非 read_text：保留原始 CRLF / BOM，避免 text 模式
                # universal-newline 翻译把 "\r\n" 折成 "\n"，否则回退时无法还原原样换行。
                before_content = resolved.read_bytes().decode("utf-8", errors="replace")
            except OSError as read_exc:
                log.warning(
                    "delete_before_read_failed",
                    extra={
                        "msg": "删除前读取文件全文失败，回退时该文件将无法重建内容",
                        "data": {
                            "path": str(resolved),
                            "error": str(read_exc),
                        },
                    },
                )
            entry.unlink()
        except OSError as exc:
            return tool_error(
                self.name,
                os_error_message(exc, "delete the target"),
                reason=(
                    "the delete failed, usually because the target is locked by another "
                    "process, the current user lacks write/delete permission, or the path "
                    "was removed concurrently. Close the program holding the file or "
                    "adjust permissions, then retry the same delete."
                ),
                retryable=True,
                permission=self.permission,
            )
        internal_data = {
            "changes": [
                {
                    "path": path,
                    "new_path": None,
                    "status": "deleted",
                    "before": before_content,
                    "after": "",
                }
            ]
        }
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=f"Deleted file: {resolved}",
            data={
                "kind": "delete-result",
                "path": path,
                "path_basename": resolved.name,
                "target_type": "file",
                "recursive": False,
            },
            internal_data=internal_data,
        )

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
                verb="删除",
                icon="trash-2",
                surface="standalone",
                expandable=False,
                expand_layout="none",
            ),
        )


def build_delete_definition() -> ToolDefinition:
    """构造 delete 工具定义。

    参数:
        无。

    返回:
        ``ToolDefinition``，供 ``ToolRegistry`` 注册。

    异常:
        无。

    副作用:
        创建 ``DeleteTool`` 实例和定义对象，不执行文件系统操作。
    """

    return DeleteTool().to_definition()
