"""LLM 适配与桥接层。

本包承载 LangChain chat model 构建与运行时消息/工具 schema 的边界转换。它不依赖
``app.service`` 编排层，只依赖 ``app.models.runtime_message`` 与 ``app.tools.schemas``，
是模型适配的单一收口位置。
"""

from app.core.llm.factory import build_chat_model

__all__ = [
    "build_chat_model",
]
