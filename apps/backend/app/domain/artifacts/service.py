"""工具执行产物的创建与查询服务。"""

from pathlib import Path
from typing import Optional

from app.domain.artifacts.files import ArtifactFileStore
from app.domain.artifacts.records import ArtifactRecord
from app.storage.crud.artifact import ArtifactStore
from app.tools.schemas import ArtifactRequest


class ArtifactService:
    """统一编排 artifact 文件和元数据写入。"""

    def __init__(self, store: ArtifactStore, files: ArtifactFileStore) -> None:
        """初始化 artifact 服务依赖。

        参数:
            store: artifact 元数据仓储。
            files: artifact 内容文件仓储。

        返回:
            无。

        异常:
            无。

        副作用:
            保存仓储依赖。
        """

        self._store = store
        self._files = files

    def create_from_request(
        self,
        request: ArtifactRequest,
        run_id: str,
        step_id: Optional[str] = None,
    ) -> ArtifactRecord:
        """从工具执行请求创建文本 artifact。

        参数:
            request: 由工具 handler 返回的产物请求。
            run_id: 所属运行标识。
            step_id: 可选关联步骤标识。

        返回:
            已落盘的 artifact 记录。

        异常:
            OSError: 当 artifact 文件无法写入时抛出。
            Exception: 当 artifact 元数据无法写入时抛出。

        副作用:
            写入 artifact 文件与 SQLite 元数据。
        """

        relative_path, size_bytes, digest = self._files.write_text(request.kind, request.content)
        return self._store.create(
            run_id=run_id,
            kind=request.kind,
            mime_type=request.mime_type,
            storage_path=relative_path,
            size_bytes=size_bytes,
            sha256=digest,
            summary=request.summary,
            step_id=step_id,
        )
