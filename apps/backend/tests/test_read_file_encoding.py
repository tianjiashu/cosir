"""read_file 编码回归测试：合法 UTF-8 正常读取，非 UTF-8 明确报错而非塞入替换字符。

这些测试覆盖之前 ``errors="replace"`` 造成的静默损坏隐患——非法字节会被悄悄替换成
U+FFFD（豆腐块），进而被回喂给模型、甚至写回文件造成乱码自我繁殖。修复后源码读取
改 ``errors="strict"``，解码失败时返回明确错误。
"""

from pathlib import Path

from app.tools.tool_handler.read_file import ReadFileTool


def test_read_valid_utf8_contains_chinese(tmp_path: Path) -> None:
    """合法 UTF-8 文件（含中文）可被正常读取。"""
    file = tmp_path / "sample.txt"
    file.write_text("第一行\n第二行中文\n", encoding="utf-8")
    result = ReadFileTool()._read_text_page(file, 1, 100)
    assert result.error == ""
    assert "第二行中文" in result.content
    assert "\ufffd" not in result.content


def test_read_non_utf8_reports_error_without_replacement_chars(tmp_path: Path) -> None:
    """非 UTF-8 文件（GBK 字节）读取失败返回错误，且结果不含替换字符。"""
    file = tmp_path / "gbk.txt"
    file.write_bytes("中文不是utf8".encode("gbk"))
    result = ReadFileTool()._read_text_page(file, 1, 100)
    assert result.error != ""
    assert "\ufffd" not in result.content
