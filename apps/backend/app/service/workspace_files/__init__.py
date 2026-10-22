"""工作区文件浏览用例包；公开服务门面与其协作类型，供上层仅依赖包接口。"""

from app.service.workspace_files.file_watch import WorkspaceFileWatcher
from app.service.workspace_files.workspace_files_service import (
    FileConflictError,
    WorkspaceFilesService,
)

__all__ = ["FileConflictError", "WorkspaceFileWatcher", "WorkspaceFilesService"]
