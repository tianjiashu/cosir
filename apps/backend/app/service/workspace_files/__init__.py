"""文件面板的后端用例：目录浏览（``directory/``）、文件内容（``content/``）与查找（``search/``），由门面
``WorkspaceFilesService`` 统一对外。

包入口只导出 API 与装配层要用的门面、轮询比对与冲突异常；交付记录等其他能力需要文件识别时直接引用
``content/`` 下的模块。
"""

from app.service.workspace_files.file_models import FileConflictError
from app.service.workspace_files.file_watch import WatchedPaths
from app.service.workspace_files.workspace_files_service import WorkspaceFilesService

__all__ = ["FileConflictError", "WatchedPaths", "WorkspaceFilesService"]
