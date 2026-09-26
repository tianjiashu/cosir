"""``app.utils.json_utils.read_json_object`` 的契约测试。"""

import json
from pathlib import Path

import pytest

from app.utils.json_utils import JsonFileError, read_json_object


def test_reads_top_level_object(tmp_path: Path) -> None:
    """合法 JSON 对象返回原始键值映射，且只读不改文件。"""

    path = tmp_path / "config.json"
    payload = {"a": 1, "b": [1, 2], "c": {"d": None}}
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert read_json_object(path) == payload
    assert path.read_text(encoding="utf-8") == json.dumps(payload)


def test_accepts_string_path(tmp_path: Path) -> None:
    """同时接受 str 路径，不要求调用方先转 Path。"""

    path = tmp_path / "config.json"
    path.write_text('{"a": 1}', encoding="utf-8")

    assert read_json_object(str(path)) == {"a": 1}


@pytest.mark.parametrize("payload", ["[1, 2]", "42", '"text"', "null", "true"])
def test_rejects_non_object_top_level(tmp_path: Path, payload: str) -> None:
    """顶层是数组或标量时抛 JsonFileError，消息带文件路径。"""

    path = tmp_path / "payload.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(JsonFileError, match="JSON 顶层必须是对象") as excinfo:
        read_json_object(path)

    assert str(path) in str(excinfo.value)


def test_rejects_invalid_json_syntax(tmp_path: Path) -> None:
    """JSON 语法非法时以 JsonFileError 报出，不泄漏底层解析异常类型。"""

    path = tmp_path / "broken.json"
    path.write_text("{", encoding="utf-8")

    with pytest.raises(JsonFileError, match="JSON 文件读取或解析失败"):
        read_json_object(path)


def test_rejects_invalid_utf8(tmp_path: Path) -> None:
    """非 UTF-8 字节序列同样归一为 JsonFileError，而不是向上抛 UnicodeDecodeError。"""

    path = tmp_path / "latin.json"
    path.write_bytes(b'{"a": "\xff\xfe"}')

    with pytest.raises(JsonFileError, match="UnicodeDecodeError"):
        read_json_object(path)


def test_rejects_missing_file(tmp_path: Path) -> None:
    """文件不存在时抛 JsonFileError，且消息保留路径供定位。"""

    path = tmp_path / "missing.json"

    with pytest.raises(JsonFileError, match="FileNotFoundError") as excinfo:
        read_json_object(path)

    assert str(path) in str(excinfo.value)
