"""Trace Backbone 使用的标识符生成与校验。"""

from uuid import uuid4


def new_trace_id() -> str:
    """生成新的 trace 标识。

    参数:
        无。

    返回:
        32 位小写十六进制 trace_id，兼容 OpenTelemetry trace id 形态。

    异常:
        无。

    副作用:
        读取系统随机源生成 UUID。
    """

    return uuid4().hex


def new_span_id() -> str:
    """生成新的 span 标识。

    参数:
        无。

    返回:
        16 位小写十六进制 span_id，兼容 OpenTelemetry span id 形态。

    异常:
        无。

    副作用:
        读取系统随机源生成 UUID。
    """

    return uuid4().hex[:16]


def new_event_id() -> str:
    """生成新的 trace event 标识。

    参数:
        无。

    返回:
        UUID 字符串，用于 trace_events 主键。

    异常:
        无。

    副作用:
        读取系统随机源生成 UUID。
    """

    return str(uuid4())


def is_trace_id(value: str) -> bool:
    """判断字符串是否符合本地 trace_id 形态。

    参数:
        value: 待校验的字符串。

    返回:
        当字符串为 32 位小写十六进制时返回 True。

    异常:
        无。

    副作用:
        无。
    """

    return (
        isinstance(value, str)
        and len(value) == 32
        and all(char in "0123456789abcdef" for char in value)
    )


def is_span_id(value: str) -> bool:
    """判断字符串是否符合本地 span_id 形态。

    参数:
        value: 待校验的字符串。

    返回:
        当字符串为 16 位小写十六进制时返回 True。

    异常:
        无。

    副作用:
        无。
    """

    return (
        isinstance(value, str)
        and len(value) == 16
        and all(char in "0123456789abcdef" for char in value)
    )
