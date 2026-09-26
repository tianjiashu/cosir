"""JSON 文件解析纯工具。

单一职责：提供「读取 JSON 文件 → 顶层 JSON 对象」这一层通用能力，且只做到这一层——
不解释任何业务字段，不做 schema 校验（字段契约由各调用方自行维护），不缓存、不写日志。
不负责：路径解析、权限判定、业务语义与降级策略。

所有失败都以 :class:`JsonFileError` 抛出并带文件路径与具体原因；需要「失败即回退默认值」
的调用方自行捕获该异常后决定降级（例如启动状态文件与能力清单的读取）。
"""

import json
from pathlib import Path
from typing import Any


class JsonFileError(ValueError):
    """表示 JSON 文件无法读取、解码或解析为顶层 JSON 对象。

    由 :func:`read_json_object` 抛出，消息中始终带有文件路径与失败原因。继承 ``ValueError``
    以贴合「输入不合法」语义，同时让调用方可以只捕获本类型而不吞掉其他 ``ValueError``。
    """


def read_json_object(path: str | Path) -> dict[str, Any]:
    """读取 UTF-8 JSON 文件并确认顶层是 JSON 对象。

    参数:
        path: JSON 文件路径（字符串或 Path 对象）。

    返回:
        顶层 JSON 对象的原始键值映射；值为 ``json.loads`` 的反序列化结果，字段级契约由
        调用方自行校验。

    异常:
        JsonFileError: 文件不存在或不可读（``OSError``）、内容不是合法 UTF-8
            （``UnicodeDecodeError``）、JSON 语法非法（``JSONDecodeError``），或顶层不是
            JSON 对象（数组/标量/字符串）；消息中包含文件路径与具体原因。

    副作用:
        读取指定文件；不修改文件、不写缓存、不写日志（需要记录的调用方在捕获异常后自行记录）。
    """

    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JsonFileError(
            f"JSON 文件读取或解析失败，文件={source}，原因={type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise JsonFileError(f"JSON 顶层必须是对象，文件={source}，实际为 {type(raw).__name__}")
    return raw
