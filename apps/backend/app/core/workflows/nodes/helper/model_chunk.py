"""模型节点「流式 chunk 解析」处理器。

把模型 ``astream`` 产出的 ``AIMessageChunk`` 流转为模型节点需要的三个结构化产物：

- 思考分片抽取（``ModelChunkProcessor.extract_reasoning``）：按厂商 thinking 通道抽思考过程文本；
- 工具调用提前抽取（``ModelChunkProcessor.extract_tool_calls``）：从单个 chunk 尽快拿到模型意图
  调用的工具集合；
- chunk 合并（``ModelChunkProcessor.collect``）：把累积的 ``AIMessageChunk`` 列表合并为标准的
  ``AIMessage``。

只承载「模型 chunk → 结构化 parts」单一职责：不触达运行时上下文、不写日志、不落外部状态（仅
``collect`` 合并后会经 ``debug_dump`` 落盘完整 chunk JSON，属调试旁路）；
全部为纯转换，便于独立测试。

构造时持有 per-run 的 ``thinking_channel``（来自 ``LLMRuntimeConfig.thinking_channel``），免去调用方
每次传通道；思考字段的回传 / 剥离策略不在本处理器处置，由下游持久化与回传边界负责（``collect``
原样透传 ``additional_kwargs``）。
"""

import copy
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk

from app.utils.message_content import content_to_text

from .debug_dump import _dump_merged_chunk_debug


class ModelChunkProcessor:
    """模型流式 chunk 解析器：把 ``AIMessageChunk`` 流转为模型节点所需的结构化产物。

    构造参数 ``thinking_channel`` 描述本轮模型使用的厂商 thinking 通道
    （``reasoning_content`` / ``thought`` / ``thinking_blocks`` / ``reasoning``），
    ``extract_reasoning`` 据此抽思考文本；其余方法（工具调用抽取、chunk 合并）与通道无关。

    本处理器只做纯转换，不写任何外部状态；``collect`` 的调试落盘属旁路，失败已降级。
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

        注意：流式分片中 ``args`` 通常是未闭合的部分 JSON（跨 chunk 拼接属于 ``collect`` 职责），
        本函数只返回「当前 chunk 可见」的完整条目，不跨 chunk 拼接。需要最终完整工具调用请在工具
        节点合并后从 ``tool_calls`` 读取。

        参数:
            chunk: 模型 ``astream`` 产出的 ``AIMessageChunk``（不要求已聚合）。

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

    def collect(self, chunks: list[AIMessageChunk]) -> AIMessage:
        """把累积的 ``AIMessageChunk`` 列表合并为标准的 ``AIMessage``。

        合并后保留 ``additional_kwargs``（含 DeepSeek 的 ``reasoning_content`` 等 provider 私有
        扩展字段）原样透传；思考内容已在流式阶段作为 ``MODEL_THINKING_DELTA`` 推送给前端，其
        回传 / 剥离策略由下游持久化与回传边界负责，本处理器不在此处置。``content`` 经
        ``content_to_text`` 抽为纯文本（防御含 ``tool_call`` block 的 list 形态，与落库口径一致），
        避免回灌模型时重复携带工具结构。合并后的完整结构会经 ``_dump_merged_chunk_debug`` 落盘到
        ``logs/debug_merged_chunks.jsonl`` 供排查。

        参数:
            chunks: 模型流式产出的分块列表（可能为空）。

        返回:
            可安全存入 graph state 并交给下一步模型调用的 ``AIMessage``：
            - ``content`` 经 ``content_to_text`` 抽为纯文本；
            - ``tool_calls`` / ``usage_metadata`` 透传（usage 由 ``add_usage`` 正确累加后的完整
              统计）；
            - ``additional_kwargs`` 原样透传（含 thinking 私有字段）；
            - ``id`` 透传 merged 的消息运行 ID（``lc_run--<uuid>``），供日志与 trace 关联；缺失时为
              ``None``。
            空输入返回空 ``AIMessage``。

        异常:
            无（chunk 合并与调试落盘均不向外抛出；落盘失败已在 ``_dump_merged_chunk_debug`` 内降级为
            warning）。

        副作用:
            经 ``_dump_merged_chunk_debug`` 向 ``Settings.LOG_DIR / debug_merged_chunks.jsonl``
            追加一行完整 chunk JSON（调试通道，不受常规日志预算截断）。
        """

        merged: AIMessageChunk | None = None
        for chunk in chunks:
            merged = chunk if merged is None else merged + chunk  # LangChain chunk 支持 + 累加
        if merged is None:
            return AIMessage(content="")  # 空输入返回空消息

        # 完整结构落调试文件（不受日志预算截断），先于常规摘要日志执行。
        _dump_merged_chunk_debug(merged)

        # additional_kwargs 原样透传：thinking 字段的剥离/回传策略由下游负责，本处理器不处置。
        additional = dict(merged.additional_kwargs) if merged.additional_kwargs else {}

        # content 统一抽纯文本：防御 DeepSeek 偶发把工具调用 block 带进 content list 的形态，
        # 与 RuntimeContextManager 落库口径保持一致，避免回灌模型时重复携带工具结构。
        return AIMessage(
            content=content_to_text(merged.content),  # 合并后的纯文本（已防御 list 形态）
            tool_calls=merged.tool_calls or [],  # 工具调用（可能为空）
            invalid_tool_calls=merged.invalid_tool_calls,
            additional_kwargs=additional,  # 原样透传 provider 私有扩展字段
            usage_metadata=merged.usage_metadata,  # 透传完整 token 统计（唯一来源）
            id=getattr(merged, "id", None),  # 消息 id 透传
            response_metadata=merged.response_metadata,
        )

    def build_transport_parts(
        self,
        chunks: list[AIMessageChunk],
        message: AIMessage,
        presentations: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """把流式顺序和完整 AIMessage 工具身份冻结为可持久化 Transport parts。

        文本与 reasoning 只从流式 chunk 读取，工具参数与身份以完整消息为准；因此未完成的
        chunk 不会被当成持久化事实，工具 presentation 则由调用方传入的运行期静态声明快照提供。
        本方法是纯转换，不写数据库或发送 Transport 事件。
        """

        parts: list[dict[str, Any]] = []
        for chunk in chunks:
            text = content_to_text(chunk.content)
            if text:
                parts.append({"type": "text", "text": text, "status": "completed"})
            reasoning = self.extract_reasoning(chunk)
            if reasoning:
                parts.append({"type": "reasoning", "text": reasoning, "status": "completed"})
            for raw_call in self.extract_tool_calls(chunk):
                call_id = raw_call.get("id")
                if not isinstance(call_id, str) or not call_id:
                    continue

        final_calls = {
            str(call.get("id")): call
            for call in (message.tool_calls or [])
            if isinstance(call.get("id"), str) and call.get("id")
        }
        for call_id, call in final_calls.items():
            args = call.get("args")
            parts.append(
                {
                    "type": "tool-call",
                    "toolCallId": call_id,
                    "toolName": str(call.get("name") or ""),
                    "status": "pending",
                    "args": copy.deepcopy(args) if isinstance(args, dict) else {},
                    "presentation": copy.deepcopy((presentations or {}).get(call_id, {})),
                    "isError": False,
                }
            )
        return parts


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
