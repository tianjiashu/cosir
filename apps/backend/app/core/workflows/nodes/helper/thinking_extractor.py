"""模型节点「思考通道抽取」纯函数集。

本模块只承载「思考提取」单一职责：按厂商 thinking 通道从模型 chunk 中抽思考过程
文本、按回传策略判断是否剥离 ``reasoning_content``。全部为纯函数，不写外部状态、
不落日志、不触达运行时上下文，便于独立测试。设计契约见模型节点 docstring
（阶段 3.2 / 3.3）。

与 chunk 组装（``chunk_assembler``）、debug 落盘（``debug_dump``）职责分离：
本模块只关心「抽出什么 / 是否剥离」，不关心「chunk 如何合并成 ``AIMessage``」。
无循环导入：本模块不 import ``model_node`` / ``common``。
"""


def _extract_reasoning_content(chunk, channel: str) -> str:
    """从 LangChain 消息 chunk 按厂商 thinking 通道提取思考过程分片。

    按 ``LLMRuntimeConfig.thinking_channels`` 顺序尝试各通道（设计文档阶段
    3.2）：``reasoning_content`` 读 ``additional_kwargs["reasoning_content"]``
    （DeepSeek / Kimi / OpenAI 兼容）；``thought`` 遍历 content 块中
    ``thought=True`` 的文本（Gemini）；``thinking_blocks`` 读
    ``additional_kwargs["thinking_blocks"]`` 中 type 为 ``thinking`` 的块文本
    （Anthropic 备选）；``reasoning`` 读 ``additional_kwargs["reasoning"]``
    摘要文本（OpenAI o 系列）。空通道或字段缺失安全返回空串。逐 token 流式
    场景下每个 chunk 携带的只是思考片段，由调用方累加到客户端。

    参数:
        chunk: 模型 ``astream`` 产出的 LangChain 消息 chunk。
        channels: 厂商思考通道元组（来自 ``LLMRuntimeConfig.thinking_channels``）。

    返回:
        思考过程文本分片；无则空串。

    异常:
        无（对缺失属性与非字符串值均安全降级为空串）。

    副作用:
        无（纯读取 chunk 字段，不写任何外部状态）。
    """

    additional = getattr(chunk, "additional_kwargs", None)  # 防止无该属性时报错
    if channel == "reasoning_content":
        if isinstance(additional, dict):
            value = additional.get("reasoning_content")
            if isinstance(value, str) and value:
                return value
    elif channel == "thought":
        text = _extract_thought_blocks(chunk.content)
        if text:
            return text
    elif channel == "thinking_blocks":
        text = _extract_thinking_blocks(additional)
        if text:
            return text
    elif channel == "reasoning":
        text = _extract_reasoning_field(additional)
        if text:
            return text
    return ""


def _extract_thought_blocks(content) -> str:
    """从 Gemini content 块中拼接 ``thought=True`` 的思考文本。

    参数:
        content: LangChain 消息的 ``content`` 字段（字符串或分块列表）。

    返回:
        全部 thought 块文本拼接；无则空串。

    异常:
        无（对非预期结构安全降级为空串）。

    副作用:
        无。
    """

    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("thought") is True:
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _extract_thinking_blocks(additional) -> str:
    """从 additional_kwargs 中提取 Anthropic thinking 块文本。

    参数:
        additional: 消息的 ``additional_kwargs``（可为 None）。

    返回:
        type 为 ``thinking`` 的块文本拼接；无则空串。

    异常:
        无（对非预期结构安全降级为空串）。

    副作用:
        无。
    """

    if not isinstance(additional, dict):
        return ""
    blocks = additional.get("thinking_blocks")
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "thinking":
            text = block.get("thinking") or block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _extract_reasoning_field(additional) -> str:
    """从 additional_kwargs 中提取 OpenAI o 系列 reasoning 摘要文本。

    参数:
        additional: 消息的 ``additional_kwargs``（可为 None）。

    返回:
        ``reasoning`` 字段文本；无则空串。

    异常:
        无（对非预期结构安全降级为空串）。

    副作用:
        无。
    """

    if not isinstance(additional, dict):
        return ""
    value = additional.get("reasoning")
    if isinstance(value, str):
        return value
    # 兼容 reasoning 为 dict（含 summary / encrypted_content）形态：仅取摘要展示。
    if isinstance(value, dict):
        summary = value.get("summary")
        if isinstance(summary, str):
            return summary
    return ""


def _should_strip_reasoning_content(
        channel: str,
        thinking_roundtrip: bool,
) -> bool:
    """判断是否剥离 assistant 消息中的 ``reasoning_content``（回传策略）。

    设计文档阶段 3.3：``reasoning_content`` 通道的厂商（DeepSeek / Kimi /
    OpenAI 兼容，通道仅含 ``reasoning_content``）保持剥离，避免重复思考；
    Anthropic（``thinking_blocks``）/ Gemini（``thought``）/ OpenAI o 系列
    （``reasoning``）需要回传 thinking 块（含 signature / encrypted_content），
    否则下一轮工具调用 400，故保留该字段。``thinking_roundtrip=False`` 时
    强制剥离（宁可丢 thinking 也不 400），不抛。

    参数:
        channels: 厂商思考通道元组。
        thinking_roundtrip: 是否启用 thinking 原样回传。

    返回:
        需要剥离 ``reasoning_content`` 时返回 True。

    异常:
        无。

    副作用:
        无。
    """

    if not thinking_roundtrip:
        return True
    # 通道为空（未解析到 LLMRuntimeConfig 的兜底）时保持既有剥离行为（安全）。
    if not channel:
        return True
    return False


__all__ = [
    "_extract_reasoning_content",
    "_extract_reasoning_field",
    "_extract_thinking_blocks",
    "_extract_thought_blocks",
    "_should_strip_reasoning_content",
]
