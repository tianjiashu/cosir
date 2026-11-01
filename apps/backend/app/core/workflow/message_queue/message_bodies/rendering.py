"""消息渲染的公共零件：标签转义、渲染时刻与带图片的 user 消息，供各类 body 复用。"""

from collections.abc import Sequence
from datetime import datetime

from openai.types.chat import (
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionUserMessageParam,
)
from openai.types.chat.chat_completion_content_part_image_param import ImageURL


def escape_tag(text: str, tag: str) -> str:
    """转义外部文本中的同名标签，防止伪造消息边界；其余内容原样保留。"""
    return text.replace(f"</{tag}", f"&lt;/{tag}").replace(f"<{tag}", f"&lt;{tag}")


def render_time() -> str:
    """渲染时刻的本地 ISO 8601（含时区偏移）。

    秒级精度足够表达消息先后，毫秒只会给模型增加噪声。取渲染时刻而不是入队时刻：body 不保存发送
    时间，同一消息在多个 Run 中渲染也只会写进 context 一次。
    """

    return datetime.now().astimezone().isoformat(timespec="seconds")


def user_message_with_images(
    text: str, image_paths: Sequence[str]
) -> ChatCompletionUserMessageParam:
    """文字加附件图片的 user 消息；没有图片时正文就是纯文字。

    图片只写本地定位符 ``cosir-attachment://<workspace 相对路径>``，发请求前才由 ``vision_input``
    解析成 data URL，context 里不落 base64。用户上传的图片与工具产出的图片共用这一个拼装点。
    """
    if not image_paths:
        return ChatCompletionUserMessageParam(role="user", content=text)
    content: list[ChatCompletionContentPartParam] = []
    if text:
        content.append(ChatCompletionContentPartTextParam(text=text, type="text"))
    content.extend(
        ChatCompletionContentPartImageParam(
            image_url=ImageURL(url=f"cosir-attachment://{path}"), type="image_url"
        )
        for path in image_paths
    )
    return ChatCompletionUserMessageParam(role="user", content=content)
