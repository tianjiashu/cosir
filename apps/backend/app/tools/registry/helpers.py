"""工具 handler 结果序列化辅助。

参考 hermes-agent 的 tool_result/tool_error：每个 handler 返回 JSON 字符串，
消除散落各处的 ``json.dumps({"error": ...})`` 样板。handler 返回字符串后由
上层 ToolObservationBuilder 直接作为 content 使用。
"""

import json
from typing import Any, Optional


def tool_result(data: Any = None, **kwargs: Any) -> str:
    """返回工具成功的 JSON 字符串结果。

    参数:
        data: 可选的 dict 位置参数（直接作为结果体）。
        kwargs: 关键字参数（与 data 二选一）。

    返回:
        JSON 字符串。

    异常:
        无。

    副作用:
        无。
    """
    if data is not None:
        return json.dumps(data, ensure_ascii=False)
    return json.dumps(kwargs, ensure_ascii=False)


def tool_error(message: str, **extra: Any) -> str:
    """返回工具失败的 JSON 字符串结果。

    参数:
        message: 错误描述。
        extra: 附加字段（如 code）。

    返回:
        JSON 字符串。

    异常:
        无。

    副作用:
        无。
    """
    result: dict = {"error": str(message)}
    if extra:
        result.update(extra)
    return json.dumps(result, ensure_ascii=False)
