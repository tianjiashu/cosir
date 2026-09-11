"""LangChain 消息 content 归一为纯文本。

本模块承载「content → text」单一职责，是模型节点与运行时上下文落库两处
归一逻辑的**唯一事实来源**。语义严格对齐 ``langchain_core.BaseMessage.text``
（源码 capability.py:262-292）：只抽取字符串块与 ``type == "text"`` 的文本块，其它
类型块（如 ``image_url``）一律忽略；``text`` 非字符串时跳过，避免 join 报错。

两处旧实现（``AIMessageChunk`` 抽取 / ``runtime_context_manager._content_to_text``）
口径不一致——后者不校验 ``type`` 会把多模态副文本混入落库，污染历史上下文。统一收口到此处后，
未来若 LangChain 调整语义只需同步一处，或直接改用 ``BaseMessage.text`` 彻底删掉本模块
（进一步零自研）。
"""

from typing import Any


def content_to_text(content: str | list[str | dict[str, Any]]) -> str:
    """把 LangChain ``BaseMessage.content`` 归一为纯文本，口径与官方 ``BaseMessage.text`` 一致。

    只抽取字符串块与 ``type == "text"`` 的文本块；其它块类型（``image_url`` 等）忽略，
    避免多模态副文本污染落库/回复文本。``text`` 字段非字符串时跳过，防止 ``"".join`` 报错。

    参数:
        content: ``BaseMessage.content`` 字段值（``str`` 或内容块列表）。

    返回:
        归一化后的纯文本字符串；无法识别时返回空字符串。

    异常:
        无。

    副作用:
        无（纯函数，不修改入参）。
    """
    if content is None:
        return ""  # LangChain content 标注允许 None，落库/回复路径需安全兜底
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
