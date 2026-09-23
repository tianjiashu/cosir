"""运行时与工作流策略共享的工作流协议，以及 LangGraph checkpointer 工厂。

Workflow 编排层强依赖 LangGraph（见 ``AGENTS.md`` 不可变决议）：``StateGraph`` 负责编排、
``AsyncSqliteSaver`` 负责 checkpoint 真实落盘。checkpoint 数据库文件路径来自
``app.storage.store_engines.checkpoint_path``，本模块只负责产出 checkpointer；未来如需替换为
其他后端（如 PostgreSQL），只需修改 ``build_checkpointer`` 与路径提供方两处。
"""

from abc import ABC
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import ClassVar

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.core.runtime.execution_mode import ExecutionMode
from app.core.workflows.workflow_operations import WorkflowOperations
from app.storage.store_engines import checkpoint_path as _engine_checkpoint_path


class AgentWorkflow(ABC):
    """定义面向运行时的、单个 Agent 工作流接口。"""

    workflow_id: ClassVar[str]

    async def run(
        self,
        operations: WorkflowOperations,
        callbacks: list | None = None,
        langfuse_trace_id: str | None = None,
        execution_mode: ExecutionMode = "fresh",
    ) -> None:
        """通过一个工作流策略运行一个任务。

        参数:
            operations: 暴露给工作流的、运行时拥有的操作。
            callbacks: 可选的 LangChain callbacks（如 Langfuse ``CallbackHandler``），
                注入 ``graph.astream`` 的 ``config["callbacks"]``，使 LLM 调用被自动追踪。
            langfuse_trace_id: 可选的 Langfuse trace 标识；工作流可在终态事件 payload
                中携带，供前端展示与跳转。未启用 Langfuse 时为 None。
            execution_mode: 本次执行是 ``fresh`` 还是从既有 checkpoint 恢复（``resume``）；
                由工作流实现决定是否清空该 run 的旧上下文与如何构造 graph 输入。

        返回:
            无（协程）。工作流只驱动领域事实写入；Transport 通过 canonical conversation
            state 订阅事实变更。

        异常:
            Exception: 工作流失败可能传播到运行时包装器。

        副作用:
            使用 ``operations`` 更新状态、调用模型、执行工具，并记录对话事实。
        """

        ...


@asynccontextmanager
async def build_checkpointer() -> AsyncIterator[AsyncSqliteSaver]:
    """构造并产出 AsyncSqliteSaver checkpointer。

    ``AsyncSqliteSaver`` 底层经 aiosqlite 直连数据库文件，不接受 SQLAlchemy 引擎；因此
    直接复用 ``app.storage.store_engines.checkpoint_path`` 提供的文件路径，连接生命周期由
    LangGraph 的 ``from_conn_string`` 上下文管理器负责释放。产出对象需以 ``async with``
    方式使用。

    生成:
        已配置好、可直接传给 ``graph.compile(checkpointer=...)`` 的 AsyncSqliteSaver。

    异常:
        RuntimeError: 如果 ``init_storage`` 尚未调用。
    """

    async with AsyncSqliteSaver.from_conn_string(_engine_checkpoint_path()) as saver:
        yield saver
