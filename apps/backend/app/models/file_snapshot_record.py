"""文件快照值对象（task 级变更集底座）。

单一职责：承载单条 ``file_snapshots`` 记录的业务视图，并提供从 ORM 行
``from_model`` 的工厂映射。不承载存储、编排或工具逻辑。
"""

from dataclasses import dataclass, field
from typing import Any

from app.storage.model.file_snapshot_model import FileSnapshotModel


@dataclass(frozen=True)
class FileSnapshotRecord:
    """一次文件操作的反向快照记录。

    ``op_json`` 为反向 ``PatchOperation`` 的序列化 JSON，单文件撤销时反序列化为
    ``PatchOperation`` 直接交给 ``apply_all_with_diff`` 应用，把文件还原到改动前。
    ``stable`` 标记变更是否已随所属 turn 结束而稳定；``status`` 记录用户对该文件
    最新变更的处理态。``id`` 为数据库自增主键，构造占位为 -1，落库由存储引擎分配。
    """

    id: int = field(default=-1)
    turn_id: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    path: str = ""
    action: str = ""
    op_json: str = ""
    seq: int = 0
    stable: int = 0
    status: str = "pending"
    reverted_at: str = ""

    @classmethod
    def from_model(cls, row: FileSnapshotModel) -> "FileSnapshotRecord":
        """从 ORM 行构造业务值对象。

        参数:
            row: ``file_snapshots`` 表的 SQLAlchemy 行对象。

        返回:
            对应的不可变 ``FileSnapshotRecord``。

        异常:
            无。

        副作用:
            无。
        """
        return cls(
            id=row.id,
            turn_id=row.turn_id,
            tool_call_id=row.tool_call_id,
            tool_name=row.tool_name,
            path=row.path,
            action=row.action,
            op_json=row.op_json,
            seq=row.seq,
            stable=row.stable,
            status=row.status,
            reverted_at=row.reverted_at,
        )

    def to_row_dict(self) -> dict[str, Any]:
        """投影为可直接落到 ``file_snapshots`` 表的列字典。

        参数:
            无。

        返回:
            含全部表列的字典，供 CRUD 层构造 ``FileSnapshotModel`` 使用。

        异常:
            无。

        副作用:
            无。
        """
        return {
            "turn_id": self.turn_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "path": self.path,
            "action": self.action,
            "op_json": self.op_json,
            "seq": self.seq,
            "stable": self.stable,
            "status": self.status,
            "reverted_at": self.reverted_at,
        }  # id 为自增主键，由存储引擎分配，不在此显式写入
