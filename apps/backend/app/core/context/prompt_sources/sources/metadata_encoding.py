"""把外部目录元数据（Skill、子 Agent、MCP 的名称与描述）写进提示词的两种写法。

共同规则：元数据可能来自仓库里别人写的文件，不能闭合提示词的层标签，也不能伪造出目录里的新条目。
"""

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


def prompt_line(text: str) -> str:
    """压成一行放进无序列表项：空白（含换行）折叠成单个空格，防止另起一行伪造条目；
    ``< >`` 与 :func:`prompt_json` 同样转义，防止闭合层标签。
    """

    return " ".join(text.split()).replace("<", "\\u003c").replace(">", "\\u003e")
