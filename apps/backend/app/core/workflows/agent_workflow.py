"""运行时与工作流策略共享的工作流协议。"""

from collections.abc import AsyncIterator
from typing import Protocol

from app.core.runtime.runtime_operations import RuntimeOperations
from app.models import TaskRecord
from app.models.runtime_event import RuntimeEvent


class AgentWorkflow(Protocol):
    """定义面向运行时的、单个 Agent 工作流接口。"""

    async def run(
        self,
        task: TaskRecord,
        operations: RuntimeOperations,
    ) -> AsyncIterator[RuntimeEvent]:
        """通过一个工作流策略运行一个任务。

        参数:
            task: 由运行时选中的任务记录。
            operations: 暴露给工作流的、运行时拥有的操作。

        生成:
            工作流运行期间产生的运行时事件。

        异常:
            Exception: 工作流失败可能传播到运行时包装器。

        副作用:
            使用 ``operations`` 来更新状态、调用模型、执行工具，并记录运行时事件。
        """

        ...
