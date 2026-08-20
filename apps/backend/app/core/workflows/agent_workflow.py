"""运行时与工作流策略共享的工作流协议。"""

from collections.abc import AsyncIterator
from typing import Protocol

from app.core.runtime.runtime_operations import RuntimeOperations
from app.models.event.runtime_event import RuntimeEvent


class AgentWorkflow(Protocol):
    """定义面向运行时的、单个 Agent 工作流接口。"""

    def run(
        self,
        operations: RuntimeOperations,
        callbacks: list | None = None,
        langfuse_trace_id: str | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        """通过一个工作流策略运行一个任务。

        参数:
            operations: 暴露给工作流的、运行时拥有的操作。
            callbacks: 可选的 LangChain callbacks（如 Langfuse ``CallbackHandler``），
                注入 ``graph.astream`` 的 ``config["callbacks"]``，使 LLM 调用被自动追踪。
            langfuse_trace_id: 可选的 Langfuse trace 标识；工作流可在终态事件 payload
                中携带，供前端展示与跳转。未启用 Langfuse 时为 None。

        生成:
            工作流运行期间产生的运行时事件。

        异常:
            Exception: 工作流失败可能传播到运行时包装器。

        副作用:
            使用 ``operations`` 更新状态、调用模型、执行工具，并记录运行时事件。
        """

        ...
