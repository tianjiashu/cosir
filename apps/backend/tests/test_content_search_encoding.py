"""content_search 编码回归测试：非 UTF-8 文件应被跳过而非塞入乱码命中。

覆盖之前 ``errors="replace"`` 的隐患——GBK 文件会被解码成替换字符并产生误导性命中。
修复后 ``search_content`` 对解码失败的文件直接跳过。
"""

from pathlib import Path

from app.tools.tool_handler.search.content_search import search_content


def test_gbk_file_is_skipped_not_matched(tmp_path: Path) -> None:
    """GBK 编码文件不被解码成乱码，也不应出现在搜索结果中。"""
    utf8_file = tmp_path / "utf8.txt"
    utf8_file.write_text("needle_utf8_token\n", encoding="utf-8")

    gbk_file = tmp_path / "gbk.txt"
    gbk_file.write_bytes("needle_中文_gbk内容".encode("gbk"))

    output, _total, _items = search_content(tmp_path, "needle")

    assert "utf8.txt" in output
    assert "gbk.txt" not in output
    assert "\ufffd" not in output
