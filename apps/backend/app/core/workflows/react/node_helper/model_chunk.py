"""模型节点「流式 chunk 解析」处理器。

把模型 ``astream`` 产出的 ``AIMessageChunk`` 流转为模型节点需要的三类结构化产物：

- 思考分片抽取（``ModelChunkProcessor.extract_reasoning``）：按厂商 thinking 通道抽思考过程文本；
- 工具调用提前抽取（``ModelChunkProcessor.extract_tool_calls``）：从单个 chunk 尽快拿到模型意图
  调用的工具集合；
- 完成原因归一化（``ModelChunkProcessor.extract_finish_reason``）：从合并后的消息读取并归一化
  Provider 的 ``finish_reason`` / ``stop_reason``，供模型节点判定终态走向。

只承载「模型 chunk → 结构化产物」单一职责：不触达运行时上下文、不写日志、不落外部状态，
全部为纯转换，便于独立测试。chunk 累积与 ``AIMessage`` 收口由
``RuntimeContextManager.add_message_chunk`` 负责（本处理器不参与）。

构造时持有 per-run 的 ``thinking_channel``（``RuntimeConfig.thinking_channel``，由 provider 能力
目录解析得到），免去调用方每次传通道；思考字段的回传 / 剥离策略不在本处理器处置，由下游持久化
与回传边界负责。
"""

from typing import Any

from langchain_core.messages import AIMessageChunk


class ModelChunkProcessor:
    """模型流式 chunk 解析器：把 ``AIMessageChunk`` 流转为模型节点所需的结构化产物。

    构造参数 ``thinking_channel`` 描述本轮模型使用的厂商 thinking 通道
    （``reasoning_content`` / ``thought`` / ``thinking_blocks`` / ``reasoning``），
    ``extract_reasoning`` 据此抽思考文本；其余方法（工具调用抽取、chunk 合并）与通道无关。

    本处理器只做纯转换，不写任何外部状态；调用方的调试落盘属旁路，失败已降级。
    """

    def __init__(self, thinking_channel: str) -> None:
        self._thinking_channel = thinking_channel

    def extract_reasoning(self, chunk: AIMessageChunk) -> str:
        """从模型 chunk 按构造时持有的 thinking 通道抽取思考过程分片。

        逐 token 流式场景下每个 chunk 仅携带思考片段，由调用方累加到客户端。空通道或字段
        缺失安全返回空串。各通道取值规则见下列私有抽取函数：

        - ``reasoning_content``：读 ``additional_kwargs["reasoning_content"]``
          （DeepSeek / Kimi / OpenAI 兼容）；
        - ``thought``：遍历 ``content`` 块中 ``thought=True`` 的文本（Gemini）；
        - ``thinking_blocks``：读 ``additional_kwargs["thinking_blocks"]`` 中 type 为
          ``thinking`` 的块文本（Anthropic 备选）；
        - ``reasoning``：读 ``additional_kwargs["reasoning"]`` 摘要文本（OpenAI o 系列）。

        参数:
            chunk: 模型 ``astream`` 产出的 LangChain 消息 chunk。

        返回:
            思考过程文本分片；无则空串。

        异常:
            无（对缺失属性与非字符串值均安全降级为空串）。

        副作用:
            无（纯读取 chunk 字段，不写任何外部状态）。
        """

        channel = self._thinking_channel
        if channel == "reasoning_content":
            additional = getattr(chunk, "additional_kwargs", None)
            if isinstance(additional, dict):
                value = additional.get("reasoning_content")
                if isinstance(value, str) and value:
                    return value
        elif channel == "thought":
            text = _extract_thought_blocks(chunk.content)
            if text:
                return text
        elif channel == "thinking_blocks":
            text = _extract_thinking_blocks(getattr(chunk, "additional_kwargs", None))
            if text:
                return text
        elif channel == "reasoning":
            text = _extract_reasoning_field(getattr(chunk, "additional_kwargs", None))
            if text:
                return text
        return ""

    def extract_tool_calls(self, chunk: AIMessageChunk) -> list[dict[str, Any]]:
        """从单个 chunk 提前抽取全部完整 tool_call（保留原生字段）。

        优先读 ``tool_call_chunks``（流式分片主要载体，可能并行多工具调用，每项自带
        ``name`` / ``args`` / ``id`` / ``index``），再回退读 ``tool_calls``（例如已聚合的
        末 chunk、``chunk_position == "last"`` 时工具调用已被解析进 ``tool_calls``）。任一口非空
        即返回该口全部条目，不做跨口去重或合并。

        注意：流式分片中 ``args`` 通常是未闭合的部分 JSON（跨 chunk 拼接由
        ``RuntimeContextManager.add_message_chunk`` 的累积合并负责），本函数只返回「当前消息可见」
        的完整条目，不跨 chunk 拼接。需要最终完整工具调用请读合并后的 ``AIMessage.tool_calls``。

        实现按属性读取（``getattr``）上述两个字段，因此参数既可以是原始 ``AIMessageChunk``，
        也可以是累积合并后的 ``AIMessage``（``model_node`` 当前传入后者）。

        参数:
            chunk: 模型 ``astream`` 产出的原始 ``AIMessageChunk``，或累积合并后的 ``AIMessage``。

        返回:
            当前 chunk 可见的全部完整工具调用（``list[dict[str, Any]]``，每项含
            ``name`` / ``args`` / ``id`` / ``index``）；无可识别工具调用时返回空列表 ``[]``
            （而非 ``None``，便于调用方直接迭代）。

        异常:
            无（对缺失属性、非预期结构、空字段均安全降级为空列表）。

        副作用:
            无（纯读取 chunk 字段，不写任何外部状态）。
        """

        tool_call_chunks = getattr(chunk, "tool_call_chunks", None)
        calls = _collect_tool_calls(tool_call_chunks)
        if calls:
            return calls
        # 回退：聚合末 chunk 已把调用解析进 tool_calls。
        return _collect_tool_calls(getattr(chunk, "tool_calls", None))

    @staticmethod
    def extract_finish_reason(message: Any) -> str | None:
        """从完整 ``AIMessage`` 提取并归一化 Provider 的完成原因。

        LangChain 通常把 OpenAI-compatible 的 ``finish_reason`` 放在
        ``response_metadata``；部分 Provider 使用 ``stop_reason``。本方法只做字段读取与
        小写归一化，不把 Provider 原始值改写进消息或 Run 事实。取消/连接中断导致没有终止
        chunk 时返回 ``None``，由模型节点按不完整响应处理。

        参数为累积合并后的完整消息，不依赖实例状态，故声明为 ``staticmethod``；与
        ``extract_reasoning`` / ``extract_tool_calls`` 同为「模型输出 → 结构化产物」的纯转换。

        参数:
            message: 累积合并后的 LangChain 消息（``AIMessage`` 或 ``AIMessageChunk``）。

        返回:
            规范化后的小写完成原因；字段缺失、类型不正确或空字符串时返回 ``None``。

        异常:
            无；非标准 Provider metadata 安全降级为 ``None``。

        副作用:
            无。
        """

        metadata = getattr(message, "response_metadata", None)
        if not isinstance(metadata, dict):
            return None
        raw_reason = metadata.get("finish_reason") or metadata.get("stop_reason")
        if not isinstance(raw_reason, str):
            return None
        normalized = raw_reason.strip().lower()
        return normalized or None


def _extract_thought_blocks(content: Any) -> str:
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


def _extract_thinking_blocks(additional: Any) -> str:
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


def _extract_reasoning_field(additional: Any) -> str:
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


def _collect_tool_calls(target: object) -> list[dict[str, Any]]:
    """从 ``tool_call_chunks`` 或 ``tool_calls`` 条目中收集工具调用字典。

    参数:
        target: ``tool_call_chunks``（``ToolCallChunk`` 字典列表）或 ``tool_calls``
            （``{"name": ..., "args": ..., "id": ..., "index": ...}`` 字典列表）；
            非 list 时安全返回空列表。

    返回:
        原始工具调用字典列表（保留 ``name`` / ``args`` / ``id`` / ``index`` 字段，
        不裁剪、不跨条目合并）；无可识别工具调用时返回空列表 ``[]``。

    副作用:
        无（仅读取，不改 ``target``）。
    """

    if not isinstance(target, list):
        return []
    calls: list[dict[str, Any]] = []
    for item in target:
        if isinstance(item, dict):
            calls.append(dict(item))
    return calls


__all__ = [
    "ModelChunkProcessor",
    "_collect_tool_calls",
    "_extract_reasoning_field",
    "_extract_thinking_blocks",
    "_extract_thought_blocks",
]
