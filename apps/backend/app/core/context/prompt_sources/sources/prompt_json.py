"""把目录元数据编码进提示词的 JSON 写法（MCP、Skill 目录共用）。"""

from __future__ import annotations

import json


def prompt_json(value: object) -> str:
    """编码成 JSON，并转义 ``& < >``，避免外部元数据闭合提示词层标签。"""

    return (
        json.dumps(value, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
