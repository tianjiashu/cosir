"""流式 assistant 草稿的进程内聚合状态。

``StreamingMessageState`` 只承载「一条尚未收口的流式草稿」在进程内的可变聚合数据：
聚合后的 ``AIMessageChunk``、该草稿在 canonical context 中的消息序号，以及刷写判定所需
的进度。草稿的合并、刷写阈值与落库由 ``RuntimeContextManager`` 负责，本模块不持有任何
行为或外部依赖，仅作为跨模块可引用的类型边界。
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import AIMessageChunk


@dataclass
class StreamingMessageState:
    """一条流式 assistant 草稿的进程内聚合状态。

    Attributes:
        chunk: 该草稿已聚合的 ``AIMessageChunk``（可能由多次 delta 合并而来）。
        sequence: 该草稿在 canonical context 中的消息序号（首次落库时确定）。
        persisted_text_length: 最近一次落库时已持久化的文本长度，用于刷写阈值判断。
        last_persisted_at: 最近一次落库的单调时钟时间戳（秒）。
    """

    chunk: AIMessageChunk
    sequence: int
    persisted_text_length: int
    last_persisted_at: float
