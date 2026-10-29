"""消息渲染的公共零件：标签转义与渲染时刻，供各类 body 复用。"""

from datetime import datetime


def escape_tag(text: str, tag: str) -> str:
    """转义外部文本中的同名标签，防止伪造消息边界；其余内容原样保留。"""
    return text.replace(f"</{tag}", f"&lt;/{tag}").replace(f"<{tag}", f"&lt;{tag}")


def render_time() -> str:
    """渲染时刻的本地 ISO 8601（含时区偏移）。

    秒级精度足够表达消息先后，毫秒只会给模型增加噪声。取渲染时刻而不是入队时刻：body 不保存发送
    时间，同一消息在多个 Run 中渲染也只会写进 context 一次。
    """

    return datetime.now().astimezone().isoformat(timespec="seconds")
