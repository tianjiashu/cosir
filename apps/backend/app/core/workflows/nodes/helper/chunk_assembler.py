"""模型节点「chunk 组装」辅助集。

本模块只承载「chunk → AIMessage + 文本」组装单一职责：把模型流式产出的
``AIMessageChunk`` 列表合并为标准的 ``AIMessage``，并提供文本抽取、有效性判断、
非法工具调用收集等配套纯函数。usage 统计不在此处解析（统一由 ``TurnUsageStats``
从 ``ai_message.usage_metadata`` 单一来源累加）。思考回传策略（剥离/保留
``reasoning_content``）来自 ``thinking_extractor``，chunk 结构 debug 落盘来自
``debug_dump``，本模块负责把三者编排成最终的 ``AIMessage``。

与思考提取（``thinking_extractor``）、debug 落盘（``debug_dump``）职责分离；
无循环导入：本模块不 import ``model_node`` / ``common``。
"""

from langchain_core.messages import AIMessage, AIMessageChunk

from app.core.workflows.nodes.helper.thinking_extractor import _should_strip_reasoning_content

from .debug_dump import _dump_merged_chunk_debug


def _extract_text(content) -> str:
    """从 LangChain 消息 content 中提取纯文本分片。

    参数:
        content: LangChain 消息的 ``content`` 字段（字符串或分块列表）。

    返回:
        拼接后的纯文本；无法识别时返回空字符串。
    """

    if isinstance(content, str):
        return content  # 普通字符串直接返回
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)  # 列表里直接是字符串
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))  # 多模态文本块 {"type":"text","text":...}
        return "".join(parts)  # 拼接所有文本片段
    return ""  # 其它类型（如图片）返回空


def _has_content(message: AIMessage) -> bool:
    """判断 LangChain 消息是否含有可落库的有效内容。

    参数:
        message: LangChain ``BaseMessage``（通常为 ``AIMessage``）。

    返回:
        消息含非空文本或至少一个工具调用时返回 True，否则返回 False。

    异常:
        无。

    副作用:
        无。
    """

    if _extract_text(message.content).strip():
        return True  # 有文本即视为有效
    tool_calls = getattr(message, "tool_calls", None)
    return bool(tool_calls)  # 有工具调用也视为有效


def _collect_chunk_to_ai_message(
    chunks: list[AIMessageChunk],
    *,
    thinking_channels: tuple[str, ...] = (),
    thinking_roundtrip: bool = True,
) -> AIMessage:
    """把累积的 ``AIMessageChunk`` 列表合并为标准的 ``AIMessage``。

    合并时会保留 ``additional_kwargs``（如 DeepSeek 的 ``reasoning_content`` 思考过程），
    否则思考内容会在落库 / 进入 graph state 时被丢弃，导致下游无法将其作为 thinking 事件推送。

    合并后的 ``merged``（``AIMessageChunk``）完整结构经实测（见 ``logs/debug_merged_chunks.jsonl``）
    形如以下字段，下游消费与调试时应按此契约解读::

        {
          "content": str,                 # 正文；stop 分支非空，tool_calls 分支可能为空串
          "additional_kwargs": {          # provider 私有扩展字段
            "reasoning_content": str      # DeepSeek 思考链；合并后保留，落库前剥离
          },
          "response_metadata": {          # 本轮元信息；累加失真细节见下方「注意」段
            "model_provider": "openai",
            "finish_reason": "stop" | "tool_calls",
            "model_name": "deepseek-v4-flash",
            "system_fingerprint": str
          },
          "type": "AIMessageChunk",
          "name": None,
          "id": "lc_run--<uuid>",         # 消息运行 ID
          "tool_calls": [                 # 结构化工具调用；非空表示要调用工具
            {"name": str, "args": dict, "id": "call_<n>_<hash>", "type": "tool_call"}
          ],
          "invalid_tool_calls": [],       # 解析失败/非法的工具调用（通常为空）
          "usage_metadata": {             # token 用量
            "input_tokens": int,
            "output_tokens": int,
            "total_tokens": int,
            "input_token_details": {"cache_read": int},   # 命中缓存的 input token 数
            "output_token_details": {"reasoning": int}    # 推理 token 数
          },
          "tool_call_chunks": [           # 流式累积的工具调用分片
            {"name": str, "args": str(json), "id": str, "index": int, "type": "tool_call_chunk"}
          ],
          "chunk_position": "last"        # 标记这是合并后的最终块
        }

    两种典型分支（由 ``finish_reason`` 区分）：
    - ``stop`` 分支：``content`` 为完整正文，``tool_calls`` / ``tool_call_chunks`` 均为空列表。
    - ``tool_calls`` 分支：``content`` 可能为空串，``tool_calls`` 含一个或多个待执行工具调用，
      ``tool_call_chunks`` 为对应的流式分片（``args`` 为 JSON 字符串、``index`` 为并行调用序号）。

    注意（实测与源码一致）：
    ``AIMessageChunk`` 累加时 ``response_metadata`` 走 ``merge_dicts``；其中字符串类型的 key
    （不在白名单内）会被 LangChain 字符串拼接。白名单为：
    ``index`` / ``id`` / ``output_version`` / ``model_provider``。
    如 ``finish_reason`` 多 chunk 累加可能拼成 ``"stopstop"``。但流式下 ``finish_reason`` 通常只在
    最后一个 chunk 出现（前序为空 dict），``merge_dicts`` 取「首个非空值优先」，故合并结果即末 chunk
    值、并不失真；稳妥可取 ``chunks[-1].response_metadata``。本函数不依赖它做分支判断，无影响。

    参数:
        chunks: 模型流式产出的分块列表（可能为空）。
        thinking_channels: 厂商思考通道元组（来自 ``LLMRuntimeConfig``）。
            决定是否剥离 ``reasoning_content``（设计文档阶段 3.3）：
            - 通道仅含 ``reasoning_content``（DeepSeek / Kimi / OpenAI 兼容）
              → 剥离，避免重复思考；
            - 通道含 ``thinking_blocks`` / ``thought`` / ``reasoning``
              （Anthropic / Gemini / OpenAI o 系列）→ 保留，供下一轮经
              ``RuntimeContextManager`` 转换原样回传（含 signature / thought）。
        thinking_roundtrip: 是否回传 thinking 块；False 时强制剥离不抛
            （宁可丢 thinking 也不 400，排查问题用兜底开关）。

    返回:
        可安全存入 graph state 并交给下一步模型调用的 ``AIMessage``：
        - ``content`` 经 ``_extract_text`` 抽为纯文本（防御含 ``tool_call`` block 的 list 形态）；
        - ``tool_calls`` / ``usage_metadata`` 透传（usage 由 ``add_usage`` 正确累加后的完整统计）；
        - ``additional_kwargs`` 按回传策略剥离或保留 ``reasoning_content``
          （思考内容已在流式阶段单独推送，剥离仅针对不需回传的厂商）；
        - ``id`` 透传 merged 的消息运行 ID（``lc_run--<uuid>``），供日志与 trace
          关联；缺失时为 None。

    异常:
        无（chunk 合并与调试落盘均不向外抛出；落盘失败已在
        ``_dump_merged_chunk_debug`` 内降级为 warning）。

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

    # 剥离思考字段按回传策略区分（设计文档阶段 3.3）：思考内容已在流式阶段作为
    # MODEL_THINKING_DELTA 推送给前端，DeepSeek/Kimi/OpenAI 兼容（仅 reasoning_content
    # 通道）回灌易引发重复思考故剥离；Anthropic/Gemini/o 系列需保留 thinking 块（含
    # signature）供下一轮原样回传，否则工具调用 400。
    additional = dict(merged.additional_kwargs) if merged.additional_kwargs else {}
    if _should_strip_reasoning_content(thinking_channels, thinking_roundtrip):
        additional.pop("reasoning_content", None)
    # content 统一抽纯文本：防御 DeepSeek 偶发把工具调用 block 带进 content list 的形态，
    # 与 RuntimeContextManager._langraph_message_to_runtime_message 落库口径保持一致，
    # 避免回灌模型时重复携带工具结构。
    return AIMessage(
        content=_extract_text(merged.content),  # 合并后的纯文本（已防御 list 形态）
        tool_calls=merged.tool_calls or [],  # 工具调用（可能为空）
        invalid_tool_calls=merged.invalid_tool_calls,
        additional_kwargs=additional,  # 保留/剥离 thinking 字段按回传策略
        # usage_metadata 是 LangChain 对各流式 chunk 经 add_usage 求和无重复后的唯一完整
        # 快照，是下游 TurnUsageStats.add_usage_metadata 的单一来源；不再存在逐 chunk 解析
        # 的第二口径（L2「重复计数」查证为伪阳性）。
        usage_metadata=merged.usage_metadata,  # 透传完整 token 统计（唯一来源）
        id=getattr(merged, "id", None),  # 消息 id 透传
    )


__all__ = [
    "_collect_chunk_to_ai_message",
    "_extract_text",
    "_has_content",
]
