"""将领域消息的字符串或多模态 content 投影为纯文本；非文本块不进入摘要。"""


def content_to_text(content: object) -> str:
    """将领域消息 content 投影为纯文本。

    只抽取字符串块与 ``type == "text"`` 的文本块；其它块类型（``image_url`` 等）忽略，
    避免多模态副文本污染落库/回复文本。``text`` 字段非字符串时跳过，防止 ``"".join`` 报错。

    参数:
        content: SDK 消息的 ``content`` 字段值（``str`` 或内容块列表）。

    返回:
        归一化后的纯文本字符串；无法识别时返回空字符串。

    异常:
        无。

    副作用:
        无（纯函数，不修改入参）。
    """
    if content is None:
        return ""  # 无内容时返回空文本
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""  # 非 str/list（如 int 等异常形态）安全兜底，避免迭代崩溃
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)
