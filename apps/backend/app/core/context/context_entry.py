"""运行时上下文条目值对象。

一条 ``ContextEntry`` 承载一条原生 LangChain 消息及其 Run 归属，是 ``core/context``
层对外表达「上下文变化」的统一载体（监听器、事务、working copy 均消费它）。
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import BaseMessage


@dataclass
class ContextEntry:
    """承载一条原生 LangChain 消息及其 Run 归属。

    参数:
        message: 将直接传给模型的 LangChain 原生消息。
        run_id: 产生该消息的 ConversationRun id；Task 级 system prompt 为 None。
    """

    message: BaseMessage
    run_id: int | None
    sequence: int
