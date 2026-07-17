"""运行产物 CRUD。"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from app.domain.artifacts.records import ArtifactRecord
from app.storage.database import create_session_factory
from app.storage.model.artifact import ArtifactModel
from app.storage.schema import initialize_app_schema


class ArtifactStore:
    """读写 artifact 元数据。"""

    def __init__(self, database_path: Path) -> None:
        """初始化 artifact 元数据仓储。

        参数:
            database_path: SQLite 数据库路径。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果 schema 初始化失败。

        副作用:
            初始化主库 schema。
        """

        self._engine = initialize_app_schema(database_path)
        self._session_factory = create_session_factory(self._engine)

    def create(
        self,
        run_id: str,
        kind: str,
        mime_type: str,
        storage_path: str,
        size_bytes: int,
        sha256: str,
        summary: str,
        step_id: Optional[str] = None,
    ) -> ArtifactRecord:
        """创建 artifact 元数据。

        参数:
            run_id: 所属运行标识符。
            kind: 产物类型。
            mime_type: MIME 类型。
            storage_path: 相对存储路径。
            size_bytes: 内容大小。
            sha256: 内容 SHA-256。
            summary: 摘要。
            step_id: 可选步骤标识符。

        返回:
            新建 artifact 记录。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            写入 artifacts 表。
        """

        now = datetime.now(timezone.utc)
        record = ArtifactRecord(str(uuid4()), run_id, step_id, kind, mime_type, storage_path, size_bytes, sha256, summary, now)
        with self._session_factory.begin() as session:
            session.add(
                ArtifactModel(
                    artifact_id=record.artifact_id,
                    run_id=record.run_id,
                    step_id=record.step_id,
                    kind=record.kind,
                    mime_type=record.mime_type,
                    storage_path=record.storage_path,
                    size_bytes=record.size_bytes,
                    sha256=record.sha256,
                    summary=record.summary,
                    created_at=record.created_at.isoformat(),
                )
            )
        return record
