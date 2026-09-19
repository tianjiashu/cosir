"""文件快照值对象（task 级变更集底座）。

单一职责：承载单条 ``file_snapshots`` 记录的业务视图，并提供从 ORM 行
``from_model`` 的工厂映射。不承载存储、编排或工具逻辑。
"""

import json
from dataclasses import dataclass, field
from typing import Any, cast

from app.models.file_snapshot_json import FileSnapshotMutationJson, FileSnapshotOpJson
from app.storage.model.file_snapshot_model import FileSnapshotModel


def _empty_file_snapshot_op_json() -> FileSnapshotOpJson:
    """Create the minimal envelope used by record instances before full projection."""

    return {"operation": {"type": "UPDATE", "operation_id": ""}, "path_states": []}


@dataclass(frozen=True)
class FileSnapshotRecord:
    """一条文件变更快照记录。

    收口后的 ``op_json`` 序列化此次文件操作涉及路径的前后状态；前镜像大对象由
    ChangeSet blob store 按内容寻址保存。执行期间的预写记录还通过 mutation 字段
    保存崩溃恢复所需的操作意图。
    ``status`` 记录用户对该文件最新变更的处理态；Run 生命周期以 Run 记录为准。
    ``id`` 为数据库自增主键，构造占位为 -1，落库由存储引擎分配。
    ``task_id`` 为快照归属任务；``seq`` 为 **task 内**递增序号（不同 task 各自从 0
    开始，task 是 seq 的命名空间与并发边界）。
    """

    id: int = field(default=-1)
    task_id: int = 0
    run_id: int = 0
    tool_call_id: str = ""
    mutation_id: str = ""
    mutation_state: str = "applied"
    mutation_json: FileSnapshotMutationJson | None = None
    path: str = ""
    op_json: FileSnapshotOpJson = field(default_factory=_empty_file_snapshot_op_json)
    seq: int = 0
    status: str = "pending"

    @classmethod
    def from_model(cls, row: FileSnapshotModel) -> "FileSnapshotRecord":
        """从 ORM 行构造业务值对象。

        参数:
            row: ``file_snapshots`` 表的 SQLAlchemy 行对象。

        返回:
            对应的不可变 ``FileSnapshotRecord``。

        异常:
            json.JSONDecodeError: ``op_json`` 或非空 ``mutation_json`` 不是合法 JSON。
            ValueError: JSON 顶层值不是对象。

        副作用:
            无。
        """
        return cls(
            id=row.id,
            task_id=row.task_id,
            run_id=row.run_id,
            tool_call_id=row.tool_call_id,
            mutation_id=row.mutation_id,
            mutation_state=row.mutation_state,
            mutation_json=(
                cast(
                    FileSnapshotMutationJson,
                    _decode_json_object(row.mutation_json, "mutation_json"),
                )
                if row.mutation_json
                else None
            ),
            path=row.path,
            op_json=cast(FileSnapshotOpJson, _decode_json_object(row.op_json, "op_json")),
            seq=row.seq,
            status=row.status,
        )

    def to_row_dict(self) -> dict[str, Any]:
        """投影为可直接落到 ``file_snapshots`` 表的列字典。

        参数:
            无。

        返回:
            含全部表列的字典，供 CRUD 层构造 ``FileSnapshotModel`` 使用。

        异常:
            TypeError: JSON 对象包含不能序列化的值。
            ValueError: JSON 对象包含 NaN 或无穷值。

        副作用:
            无。
        """
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "tool_call_id": self.tool_call_id,
            "mutation_id": self.mutation_id,
            "mutation_state": self.mutation_state,
            "mutation_json": (
                _encode_json(self.mutation_json) if self.mutation_json is not None else ""
            ),
            "path": self.path,
            "op_json": _encode_json(self.op_json),
            "seq": self.seq,
            "status": self.status,
        }  # id 为自增主键，由存储引擎分配，不在此显式写入


def _decode_json_object(value: str, field_name: str) -> dict[str, Any]:
    """将 ORM JSON 文本解析为对象，并拒绝非对象顶层值。"""

    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError(f"file snapshot {field_name} must contain a JSON object")
    return decoded


def _encode_json(value: object) -> str:
    """将快照 JSON 对象编码为稳定、可读的数据库文本。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
