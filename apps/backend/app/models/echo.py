"""在外部模型凭证就绪之前使用的本地流式模型适配器。"""

import asyncio
from typing import AsyncIterator, List, Optional

from app.models.base import ModelDelta, ModelToolDefinition, RuntimeMessage


class EchoStreamingModelAdapter:
    """为本地运行时验证流式产出确定性的响应。"""

    async def stream(
        self,
        messages: List[RuntimeMessage],
        tools: Optional[List[ModelToolDefinition]] = None,
    ) -> AsyncIterator[ModelDelta]:
        """为最新的用户消息流式产出确定性的助手响应。

        参数:
            messages: 至少包含一个用户消息的运行时消息。
            tools: 该本地适配器忽略的、可选的面向模型的工具定义。

        生成:
            共同组成最终助手响应的 ModelDelta 分块。

        异常:
            ValueError: 如果 ``messages`` 中不存在用户消息。

        副作用:
            在分块之间让出事件循环，以体现流式行为。
        """

        user_messages = [message for message in messages if message.role == "user"]
        if not user_messages:
            raise ValueError("at least one user message is required")

        response = f"收到任务：{user_messages[-1].content_text}"
        for token in response.split(" "):
            await asyncio.sleep(0)
            yield ModelDelta(text=f"{token} ")
        yield ModelDelta(text="", is_final=True)
