"""快照记录 → 反向 PatchOperation 反序列化。

单一职责：把 ``file_snapshots.op_json`` 中序列化的反向 V4A 操作重建为
``PatchOperation`` 对象列表，供 ``revert_file`` 喂给 ``apply_all_with_diff``。
只做纯数据转换，不读写文件、不查询数据库。
"""

import json

from app.models.file_snapshot_record import FileSnapshotRecord
from app.core.tools.tool_handler.patch.patch_parser import (
    Hunk,
    HunkLine,
    OperationType,
    PatchOperation,
)


def snapshots_to_operations(
    snapshots: list[FileSnapshotRecord],
) -> list[PatchOperation]:
    """把快照记录反序列化为反向 PatchOperation 列表（已按 seq 降序）。

    参数:
        snapshots: 已按 seq 降序的快照记录。

    返回:
        可直接喂给 ``apply_all_with_diff`` 的反向 PatchOperation 列表。

    异常:
        无。

    副作用:
        无。
    """
    operations: list[PatchOperation] = []
    for snap in snapshots:
        data = json.loads(snap.op_json)
        # 序列化时 operation 已落为 value 字符串、hunks 展开为 {"lines":[...]}，
        # 此处重建回 PatchOperation 的 Enum 与嵌套 dataclass 结构。
        hunks = [
            Hunk(
                lines=[
                    HunkLine(prefix=line["prefix"], content=line["content"])
                    for line in hunk["lines"]
                ]
            )
            for hunk in data.get("hunks", [])
        ]
        operations.append(
            PatchOperation(
                operation=OperationType(data["operation"]),
                file_path=data["file_path"],
                new_path=data.get("new_path"),
                hunks=hunks,
                content=data.get("content"),
            )
        )
    return operations
