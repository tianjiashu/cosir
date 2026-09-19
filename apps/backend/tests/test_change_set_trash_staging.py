"""trash 暂存区原语与删除前状态流式捕获的单元测试。

覆盖：trash 引用的解析/构造往返、move/restore/purge 落盘动作，以及
``capture_path(read_content=False)`` 不把大文件正文载入内存却仍给出正确 ``sha256``。
"""

import hashlib

from app.core.tools.guard.file_mutation_state import capture_path
from app.core.tools.guard.trash_staging import (
    move_path_to_trash,
    parse_trash_ref,
    purge_trash,
    resolve_trash_path,
    restore_from_trash,
    trash_restore_ref,
    trash_root_for,
)


def test_trash_ref_round_trip() -> None:
    ref = trash_restore_ref("mut_abc", "src/old.txt")
    assert ref == "trash:mut_abc/src/old.txt"
    assert parse_trash_ref(ref) == ("mut_abc", "src/old.txt")


def test_parse_trash_ref_rejects_non_trash() -> None:
    assert parse_trash_ref("sha256:deadbeef") is None
    assert parse_trash_ref("trash:") is None
    assert parse_trash_ref("trash:opid") is None  # 缺少 relative 分隔符
    assert parse_trash_ref("trash:op\\id") is None  # operation_id 含反斜杠非法


def test_resolve_trash_path(tmp_path) -> None:
    ref = trash_restore_ref("mut_x", "nested/file.txt")
    resolved = resolve_trash_path(tmp_path, ref)
    assert resolved == trash_root_for(tmp_path, "mut_x") / "nested" / "file.txt"


def test_move_restore_and_purge(tmp_path) -> None:
    source = tmp_path / "a.txt"
    source.write_bytes(b"payload")
    operation_id = "mut_1"
    trash_target = trash_root_for(tmp_path, operation_id) / "a.txt"

    move_path_to_trash(source, trash_target)
    assert not source.exists()
    assert trash_target.exists()
    assert trash_target.read_bytes() == b"payload"

    restored = tmp_path / "a.txt"
    restore_from_trash(trash_target, restored)
    assert restored.exists()
    assert restored.read_bytes() == b"payload"
    # 回退完成后操作暂存根目录被清理。
    assert not trash_root_for(tmp_path, operation_id).exists()

    # 重新建一份并验证 keep 的 purge。
    trash_target = trash_root_for(tmp_path, operation_id) / "a.txt"
    move_path_to_trash(restored, trash_target)
    purge_trash(tmp_path, operation_id)
    assert not trash_root_for(tmp_path, operation_id).exists()


def test_capture_path_without_read_content_skips_memory(tmp_path) -> None:
    payload = bytes((i % 251) for i in range(2_000_000))
    target = tmp_path / "big.bin"
    target.write_bytes(payload)

    captured = capture_path(tmp_path, target, read_content=False)
    assert captured.content is None
    assert captured.state["entry_type"] == "file"
    assert captured.state["sha256"] == hashlib.sha256(payload).hexdigest()

    # 对照：默认行为仍把正文载入内存。
    captured_full = capture_path(tmp_path, target)
    assert captured_full.content == payload
    assert captured_full.state["sha256"] == captured.state["sha256"]
