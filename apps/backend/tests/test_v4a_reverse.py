"""``v4a_reverse`` 纯函数单元测试。

覆盖对象为 ``app.tools.tool_handler.patch.v4a_reverse`` 的
``build_forward_operations`` 与 ``reverse_v4a_operation``。这两个函数是变更集
采集链路的纯转换（采集快照 → 反向 PatchOperation），不读写文件；本文件从
「迁移自 test_turn_revert 时丢失的纯函数用例」补回，聚焦四类 status 映射、
MOVE 正反路径交换、UPDATE 的 before/after 还原与 CRLF 字节保留。

端到端 apply 用例会临时写入 tmp_path 下的文件，路径越界由
``ProjectPathResolver`` 在解析阶段强制拒绝。
"""

import os
from pathlib import Path

from app.tools.tool_handler.patch.patch_apply import apply_all_with_diff
from app.tools.tool_handler.patch.patch_parser import OperationType
from app.tools.tool_handler.patch.v4a_reverse import (
    build_forward_operations,
    reverse_v4a_operation,
)
from app.tools.tool_handler.security.project_path import ProjectPathResolver


def test_build_forward_maps_four_statuses() -> None:
    """added / deleted / moved / modified 四类采集快照分别映射为 ADD/DELETE/MOVE/UPDATE。"""
    changes = [
        {"path": "new.txt", "status": "added", "before": "", "after": "hello"},
        {"path": "old.txt", "status": "deleted", "before": "bye", "after": ""},
        {"path": "src.txt", "new_path": "dst.txt", "status": "moved", "before": "m", "after": "m"},
        {"path": "mod.txt", "status": "modified", "before": "v1", "after": "v2"},
    ]
    ops = build_forward_operations(changes)

    assert [op.operation for op in ops] == [
        OperationType.ADD,
        OperationType.DELETE,
        OperationType.MOVE,
        OperationType.UPDATE,
    ]
    # ADD 必须携带 after 全文，供反向 DELETE 重建文件。
    assert ops[0].content == "hello"
    # DELETE 必须携带 before 全文，供反向 ADD 精确重建。
    assert ops[1].content == "bye"
    # MOVE 必须带 new_path。
    assert ops[2].file_path == "src.txt"
    assert ops[2].new_path == "dst.txt"
    # UPDATE 必须用 reverse_content 携带 before 原文（供反向整文件覆盖还原）。
    assert ops[3].reverse_content == "v1"


def test_reverse_maps_add_delete_and_swap() -> None:
    """ADD↔DELETE 互为反向，MOVE 路径对调。"""
    add = build_forward_operations(
        [{"path": "a.txt", "status": "added", "before": "", "after": "x"}]
    )[0]
    assert reverse_v4a_operation(add).operation == OperationType.DELETE

    delete = build_forward_operations(
        [{"path": "a.txt", "status": "deleted", "before": "x", "after": ""}]
    )[0]
    rev = reverse_v4a_operation(delete)
    assert rev.operation == OperationType.ADD
    assert rev.content == "x"

    move = build_forward_operations(
        [{"path": "src.txt", "new_path": "dst.txt", "status": "moved", "before": "m", "after": "m"}]
    )[0]
    rev_move = reverse_v4a_operation(move)
    assert rev_move.operation == OperationType.MOVE
    assert rev_move.file_path == "dst.txt"
    assert rev_move.new_path == "src.txt"


def test_reverse_update_carries_before_content() -> None:
    """UPDATE 反向必须保留 before 原文，供 apply 走整文件覆盖路径精确还原。"""
    update = build_forward_operations(
        [{"path": "mod.txt", "status": "modified", "before": "v1", "after": "v2"}]
    )[0]
    rev = reverse_v4a_operation(update)

    assert rev.operation == OperationType.UPDATE
    assert rev.file_path == "mod.txt"
    # 反向 UPDATE 的 content 应为 before 原文（apply 用它整文件覆盖还原，而非 fuzzy 行匹配）。
    assert rev.content == "v1"


def test_reverse_add_restores_content_with_trailing_newline() -> None:
    """DELETE→ADD 反向在 content 缺失时回退到 hunk 拼接，但含尾换行需由 content 精确保留。"""
    delete = build_forward_operations(
        [{"path": "a.txt", "status": "deleted", "before": "line1\nline2\n", "after": ""}]
    )[0]
    rev = reverse_v4a_operation(delete)
    # content 优先携带完整 before（含尾换行），不依赖 hunk 拼接丢失尾换行。
    assert rev.content == "line1\nline2\n"


def test_apply_update_reverse_restores_content(tmp_path: Path) -> None:
    """经 build_forward → reverse 产出的 UPDATE 反向操作，用 before 原文整文件覆盖还原。

    说明：UPDATE 反向的 content 精确携带 before 原文；写入时 atomic_write_text 默认按
    「目标文件既有行尾」归一化（preserve_eol），因此 CRLF 精确保留仅由 DELETE→ADD 的
    新建路径保证（见 test_revert_file_preserves_exact_bytes），UPDATE 反向锁定的是
    内容覆盖还原而非行尾字节。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "edit.txt"
    before = "line1\nline2\n"
    after = "changed\n"
    target.write_bytes(after.encode("utf-8"))
    resolver = ProjectPathResolver(workspace)

    forward = build_forward_operations(
        [{"path": "edit.txt", "status": "modified", "before": before, "after": after}]
    )[0]
    reverse_op = reverse_v4a_operation(forward)
    assert reverse_op.content == before
    apply_all_with_diff([reverse_op], resolver)

    assert target.read_bytes() == before.encode("utf-8")


def test_apply_move_reverse_swaps_paths(tmp_path: Path) -> None:
    """MOVE 反向后 apply 应把文件移回原路径。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    src = workspace / "src.txt"
    dst = workspace / "dst.txt"
    src.write_bytes(b"content")
    resolver = ProjectPathResolver(workspace)

    forward = build_forward_operations(
        [
            {
                "path": "src.txt",
                "new_path": "dst.txt",
                "status": "moved",
                "before": "content",
                "after": "content",
            }
        ]
    )[0]
    # 模拟正向已执行：src 已移到 dst。
    os.replace(src, dst)
    assert dst.exists() and not src.exists()

    reverse_op = reverse_v4a_operation(forward)
    assert reverse_op.operation == OperationType.MOVE
    apply_all_with_diff([reverse_op], resolver)

    assert src.read_bytes() == b"content"
    assert not dst.exists()
