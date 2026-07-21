"""在模型调用前校验运行时消息大小。"""

from app.models.runtime_message import RuntimeMessage


class ContextBudgetExceeded(RuntimeError):
    """表示模型上下文超出了第一版所配置的预算。"""


def measure_message_chars(messages: list[RuntimeMessage]) -> int:
    """使用字符计数代理来测量运行时消息的文本大小。

    参数:
        messages: 将被发送给模型适配器的运行时消息。

    返回:
        消息文本与字符串类型元数据值的总字符数。

    异常:
        无。

    副作用:
        无。
    """

    total = 0
    for message in messages:
        total += len(message.content_text)
        for value in message.metadata.values():
            if isinstance(value, str):
                total += len(value)
    return total


def validate_context_budget(
    messages: list[RuntimeMessage],
    max_context_chars: int,
) -> None:
    """当运行时消息超出所配置的上下文预算时抛出。

    参数:
        messages: 为模型适配器准备的运行时消息。
        max_context_chars: 允许的最大测量字符数。

    返回:
        无。

    异常:
        ValueError: 如果 ``max_context_chars`` 小于 1。
        ContextBudgetExceeded: 如果测量的消息文本超出预算。

    副作用:
        无。
    """

    if max_context_chars < 1:
        raise ValueError("max_context_chars must be greater than zero")

    measured_chars = measure_message_chars(messages)
    if measured_chars > max_context_chars:
        raise ContextBudgetExceeded(
            "context_window_exceeded "
            f"measured_chars={measured_chars} max_context_chars={max_context_chars}"
        )
