"""保留 OpenAI-compatible thinking 流式字段的 ChatOpenAI 适配器。

``ChatOpenAI`` 面向 OpenAI 官方响应契约，不会保留第三方兼容端点返回的
``reasoning_content``。本适配器只补齐这一处响应字段，不复制 ChatOpenAI 的请求、工具调用、
usage 或重试逻辑；普通消息转换仍完全委托父类完成。
"""

from typing import Any

from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI


class ReasoningChatOpenAI(ChatOpenAI):
    """保留 OpenAI-compatible 流式响应中的 ``reasoning_content``。

    该类只处理 Chat Completions 流式 delta 的兼容字段。请求参数、文本、工具调用、usage
    和错误处理仍由 ``ChatOpenAI`` 负责；上层通过标准 LangChain ``AIMessageChunk`` 接收
    ``additional_kwargs["reasoning_content"]``，无需感知原始供应商响应结构。
    """

    def bind_tools(
        self,
        tools,
        *,
        tool_choice=None,
        strict=None,
        parallel_tool_calls=None,
        response_format=None,
        **kwargs: Any,
    ) -> Runnable:
        """将工具配置绑定到模型，并保留 ``ChatOpenAI`` 的标准处理逻辑。

        参数:
            tools: LangChain 支持的工具、可调用对象、Pydantic 类型或 OpenAI-compatible
                工具 schema。
            tool_choice: 工具选择策略，语义与 ``ChatOpenAI.bind_tools`` 相同。
            strict: 是否启用严格工具 schema 校验。
            parallel_tool_calls: 是否允许并行工具调用。
            response_format: 可选的结构化输出 schema。
            kwargs: 透传给父类绑定逻辑的其他参数。

        返回:
            父类生成的 ``Runnable``，其中包含规范化后的工具配置。

        副作用:
            不发起网络请求，也不写入持久化状态；仅创建带工具绑定配置的 Runnable。
        """

        return super().bind_tools(
            tools,
            tool_choice=tool_choice,
            strict=strict,
            parallel_tool_calls=parallel_tool_calls,
            response_format=response_format,
            **kwargs,
        )

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict[str, Any],
        default_chunk_class: type,
        base_generation_info: dict[str, Any] | None,
    ) -> ChatGenerationChunk | None:
        """在父类完成标准转换后补回 ``delta.reasoning_content``。

        参数:
            chunk: OpenAI-compatible Chat Completions 原始流式响应。
            default_chunk_class: LangChain 默认消息 chunk 类型。
            base_generation_info: 父类转换使用的基础 generation 元数据。

        返回:
            父类生成的 ``ChatGenerationChunk``；当父类返回空结果时返回 None。

        异常:
            透传父类标准转换过程抛出的异常。

        副作用:
            仅修改当前 generation chunk 内的 ``AIMessageChunk.additional_kwargs``，不写外部状态。
        """

        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk,
            default_chunk_class,
            base_generation_info,
        )
        if generation_chunk is None or not isinstance(generation_chunk.message, AIMessageChunk):
            return generation_chunk

        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            nested_chunk = chunk.get("chunk")
            choices = nested_chunk.get("choices") if isinstance(nested_chunk, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return generation_chunk

        delta = choices[0].get("delta")
        if not isinstance(delta, dict):
            return generation_chunk
        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            generation_chunk.message.additional_kwargs["reasoning_content"] = reasoning
        return generation_chunk
__all__ = ["ReasoningChatOpenAI"]
