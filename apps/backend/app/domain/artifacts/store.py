"""运行产物 SQLite 元数据仓储。"""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Iterator, Optional
from uuid import uuid4

from app.domain.artifacts.records import ArtifactRecord
from app.storage.migrations import ensure_durable_schema


class ArtifactStore:
    """读写 artifact 元数据。"""

    def __init__(self, database_path: Path) -> None:
        """初始化 artifact 元数据仓储。

        参数:
            database_path: SQLite 数据库路径。

        返回:
            无。

        异常:
            sqlite3.Error: 如果 schema 初始化失败。

        副作用:
            初始化 artifacts 表。
        """

        self._database_path = database_path
        ensure_durable_schema(database_path)

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
            sqlite3.Error: 如果写入失败。

        副作用:
            写入 artifacts 表。
        """

        now = datetime.now(timezone.utc)
        record = ArtifactRecord(
            artifact_id=str(uuid4()),
            run_id=run_id,
            step_id=step_id,
            kind=kind,
            mime_type=mime_type,
            storage_path=storage_path,
            size_bytes=size_bytes,
            sha256=sha256,
            summary=summary,
            created_at=now,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO artifacts(
                    artifact_id, run_id, step_id, kind, mime_type, storage_path,
                    size_bytes, sha256, summary, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.artifact_id,
                    record.run_id,
                    record.step_id,
                    record.kind,
                    record.mime_type,
                    record.storage_path,
                    record.size_bytes,
                    record.sha256,
                    record.summary,
                    record.created_at.isoformat(),
                ),
            )
        return record

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """打开 SQLite 连接。

        参数:
            无。

        返回:
            可在 with 语句中使用的 SQLite 连接迭代器，连接启用了 Row 工厂。

        异常:
            sqlite3.Error: 如果连接失败。

        副作用:
            打开数据库连接；正常退出时提交事务，异常退出时回滚事务，最终关闭连接。
        """

        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
