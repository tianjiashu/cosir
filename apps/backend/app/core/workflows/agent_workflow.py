"""运行时与工作流策略共享的工作流协议。"""

from typing import ClassVar, Protocol

from app.core.runtime.runtime_operations import RuntimeOperations


class AgentWorkflow(Protocol):
    """定义面向运行时的、单个 Agent 工作流接口。"""

    # 工作流唯一标识（类级常量）：消费方（如 AgentProfile 导出）以此归类工作流类型。
    # 声明在 Protocol 中使「漏定义 workflow_id」成为类型错误，杜绝 getattr 静默回退的
    # 隐式契约（历史 L6 缺陷：消费方曾用 getattr(self.workflow,"workflow_id","custom")）。
    workflow_id: ClassVar[str]

    async def run(
        self,
        operations: RuntimeOperations,
        callbacks: list | None = None,
        langfuse_trace_id: str | None = None,
    ) -> None:
        """通过一个工作流策略运行一个任务。

        参数:
            operations: 暴露给工作流的、运行时拥有的操作。
            callbacks: 可选的 LangChain callbacks（如 Langfuse ``CallbackHandler``），
                注入 ``graph.astream`` 的 ``config["callbacks"]``，使 LLM 调用被自动追踪。
            langfuse_trace_id: 可选的 Langfuse trace 标识；工作流可在终态事件 payload
                中携带，供前端展示与跳转。未启用 Langfuse 时为 None。

        生成:
            无。工作流只驱动领域事实写入；Transport 通过 canonical conversation state
            订阅事实变更。

        异常:
            Exception: 工作流失败可能传播到运行时包装器。

        副作用:
            使用 ``operations`` 更新状态、调用模型、执行工具，并记录对话事实。
        """

        ...
