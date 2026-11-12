"""命令输出解码：逐行判定编码，一个坏字节或换了编码的一行不会把之后的输出解成乱码。"""

from __future__ import annotations

from app.tools.terminal_runtime.command.output_decoder import IncrementalOutputDecoder


def _decode(chunks: list[bytes]) -> list[str]:
    decoder = IncrementalOutputDecoder()
    parts = [decoder.decode(chunk) for chunk in chunks]
    parts.append(decoder.decode(b"", final=True))
    return parts


def test_a_line_that_looks_like_gbk_does_not_lock_the_rest_of_the_stream() -> None:
    # 0xE9 0x61 碰巧是合法 GBK：这一行按 GBK 解，下一行的 UTF-8 照常解对。
    assert "".join(_decode([b"caf\xe9a ok\n", "中文\n".encode()])).endswith("中文\n")


def test_lines_in_different_encodings_each_decode_correctly() -> None:
    gbk_then_utf8 = _decode(["拒绝访问\n".encode("gbk"), "✔ built 完成\n".encode()])
    assert "".join(gbk_then_utf8) == "拒绝访问\n✔ built 完成\n"
    utf8_then_gbk = _decode(["✔ built 完成\n".encode(), "拒绝访问\n".encode("gbk")])
    assert "".join(utf8_then_gbk) == "✔ built 完成\n拒绝访问\n"


def test_a_complete_character_is_released_without_waiting_for_the_rest_of_the_chunk() -> None:
    data = "中文输出".encode()
    assert _decode([data[:4], data[4:7], data[7:11], data[11:]])[:4] == ["中", "文", "输", "出"]


def test_gbk_split_across_chunks_still_decodes() -> None:
    data = "错误：找不到文件\n".encode("gbk")
    assert "".join(_decode([data[:1], data[1:5], data[5:]])) == "错误：找不到文件\n"


def test_an_unfinished_character_at_the_end_of_the_stream_is_replaced() -> None:
    assert _decode([b"abc\xe4"]) == ["abc", "�"]
