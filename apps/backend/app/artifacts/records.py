"""运行产物记录。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ArtifactRecord:
    """表示一个运行产物。

    参数:
        artifact_id: 产物标识符。
        run_id: 所属运行标识符。
        step_id: 关联步骤标识符。
        kind: 产物类型。
        mime_type: 内容 MIME 类型。
        storage_path: 相对存储路径。
        size_bytes: 内容字节数。
        sha256: 内容 SHA-256。
        summary: 人类可读摘要。
        created_at: 创建时间。

    返回:
        不可变产物记录。

    异常:
        无。

    副作用:
        无。
    """

    artifact_id: str
    run_id: str
    step_id: Optional[str]
    kind: str
    mime_type: str
    storage_path: str
    size_bytes: int
    sha256: str
    summary: str
    created_at: datetime

    def to_dict(self) -> Dict[str, Any]:
        """转换为 API 可序列化字典。

        参数:
            无。

        返回:
            产物记录字典。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "artifact_id": self.artifact_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "kind": self.kind,
            "mime_type": self.mime_type,
            "storage_path": self.storage_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "summary": self.summary,
            "created_at": self.created_at.isoformat(),
        }
