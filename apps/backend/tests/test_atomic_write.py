"""atomic_write 原子落盘单元测试。

守护：
1. P1-12：写入后 flush + fsync 再 os.replace——产物内容正确、无半写残留。
2. 原子替换语义：第二次写入完整覆盖第一次，目标文件内容始终为新内容，
   同目录不残留临时文件。
"""

from pathlib import Path

from app.tools.tool_handler.file_io.atomic_write import atomic_write_text


def test_write_creates_file_with_exact_content(tmp_path: Path) -> None:
    """新建文件：内容原样落盘。"""

    target = tmp_path / "a.txt"
    atomic_write_text(target, "hello world\n")
    assert target.read_text(encoding="utf-8") == "hello world\n"


def test_overwrite_replaces_content_atomically(tmp_path: Path) -> None:
    """替换已有文件：新内容完整覆盖旧内容，不残留临时文件。"""

    target = tmp_path / "b.txt"
    atomic_write_text(target, "old content\n")
    atomic_write_text(target, "new content\n")
    assert target.read_text(encoding="utf-8") == "new content\n"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".tmp_write_")]
    assert leftovers == []


def test_write_into_nested_directory_creates_parents(tmp_path: Path) -> None:
    """父目录不存在时自动创建。"""

    target = tmp_path / "nested" / "deep" / "c.txt"
    atomic_write_text(target, "nested\n")
    assert target.read_text(encoding="utf-8") == "nested\n"


def test_repeated_writes_leave_no_partial_state(tmp_path: Path) -> None:
    """多次原子写后：目标完整、目录内仅目标文件（无临时残留）。"""

    target = tmp_path / "d.txt"
    for i in range(5):
        atomic_write_text(target, f"round {i}\n")
    assert target.read_text(encoding="utf-8") == "round 4\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["d.txt"]
