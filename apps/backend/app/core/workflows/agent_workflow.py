"""跨「运行时 ↔ 工作流」边界的共享协议：工作流接口、失败契约与 checkpointer 工厂。

本模块只放边界上的协议与基础设施工厂，不承载任何具体执行策略（策略见
``app.core.workflows.react``）。这样 ``core/runtime`` 只依赖本模块即可驱动任意工作流策略，
不必反向依赖某个工作流包。

- ``AgentWorkflow``：运行时看到的「单个 Agent 的执行策略」。
- ``WorkflowRunFailure``：工作流在自身职责内已能判定失败语义时携带稳定 code 抛出，运行时直接
  用它落定 failed，不再对业务语义做二次猜测。
- ``build_checkpointer``：编排层强依赖 LangGraph——``StateGraph`` 负责编排、``AsyncSqliteSaver``
  负责 checkpoint 真实落盘。checkpoint 数据库文件路径来自
  ``app.storage.store_engines.checkpoint_path``；未来如需换后端（如 PostgreSQL），只需修改
  ``build_checkpointer`` 与路径提供方两处。
"""

from abc import ABC
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import ClassVar

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.core.runtime.execution_mode import ExecutionMode
from app.core.tools.schemas.user_decision import UserDecision
from app.core.workflows.workflow_operations import WorkflowOperations
from app.storage.store_engines import checkpoint_path as _engine_checkpoint_path


class WorkflowRunFailure(Exception):
    """携带稳定 Run 失败码、从工作流传给运行时的异常。

    工作流在自身职责内已能判定失败语义时（运行期模型解析失败、续跑时 checkpoint 已走到
    END 等）抛出本异常并带上 ``failure_code``；运行时捕获后直接用该 code 落定 Run failed
    终态，不再对业务语义做二次猜测分类。其余异常不带 code，由运行时按模型失败分类器归类。

    参数:
        failure_code: 稳定失败 code（``Constant.Run.RUN_FAILURE_CODE_*`` 或 ``ErrorKind``
            的模型错误分类值），同时作为 ``conversation_runs.end_reason`` 与受控错误 ``code``。
        message: 面向排查的失败说明，不直接展示给用户。
    """

    def __init__(self, failure_code: str, message: str) -> None:
        super().__init__(message)
        self.failure_code = failure_code


class AgentWorkflow(ABC):
    """单个 Agent 工作流的运行时接口（``workflow_id`` + ``run``）。

    接口只表达「如何执行一次 Run」，不提供模型、工具或持久化能力；工具执行与 Run 状态迁移统一经
    ``WorkflowOperations`` 门面，具体实现自行承担其余运行期装配（模型解析、checkpointer、task 级
    上下文管理器，见 ``ReactLikeWorkflow``）。

    终态归属：正常收束（completed）、协作取消与等待用户输入由实现内的节点落定到 Run；
    异常路径不在此接口内约定收口（见 :meth:`run` 的失败契约）。
    """

    workflow_id: ClassVar[str]

    async def run(
        self,
        operations: WorkflowOperations,
        callbacks: list | None = None,
        langfuse_trace_id: str | None = None,
        execution_mode: ExecutionMode = "fresh",
        user_decisions: Sequence[UserDecision] | None = None,
    ) -> None:
        """通过一个工作流策略运行一个任务。

        参数:
            operations: 暴露给工作流的、运行时拥有的操作。
            callbacks: 可选的 LangChain callbacks（如 Langfuse ``CallbackHandler``），
                注入 ``graph.astream`` 的 ``config["callbacks"]``，使 LLM 调用被自动追踪。
            langfuse_trace_id: 本次 Run 的 Langfuse trace 标识，未启用 tracing 时为 None。
                运行时已自行把该标识持久化到 Run，实现无需再处理，只按需透传给节点。
            execution_mode: 本次执行是 ``fresh``（新建输入）还是从既有 checkpoint 恢复
                （``resume`` / ``resume_with_input``）；实现据此决定是否清空该 run 的旧上下文、
                以及构造什么 graph 输入。
            user_decisions: 本次续跑携带的用户结构化决定（human-in-the-loop）；只有恢复
                human-in-the-loop 断点的实现会消费它，为空表示用户尚未作答。

        返回:
            无。工作流完成或在 interrupt 处挂起均通过图执行生命周期表达。

        异常:
            WorkflowRunFailure: 实现已能判定失败语义时携带稳定 code 抛出。
            Exception: 其余异常由实现原样上抛，由运行时统一收敛为 Run failed 终态。
            asyncio.CancelledError: 取消语义保留，不得在实现内吞掉。

        副作用:
            调用模型、执行工具并记录对话事实；用户输入等待节点直接经操作门面迁移 Run 状态。
        """

        ...


@asynccontextmanager
async def build_checkpointer() -> AsyncIterator[AsyncSqliteSaver]:
    """构造并产出 AsyncSqliteSaver checkpointer。

    ``AsyncSqliteSaver`` 底层经 aiosqlite 直连数据库文件，不接受 SQLAlchemy 引擎；因此直接复用
    ``app.storage.store_engines.checkpoint_path`` 提供的文件路径，连接生命周期由 LangGraph 的
    ``from_conn_string`` 上下文管理器负责释放。产出对象需以 ``async with`` 方式使用。

    生成:
        已配置好、可直接传给 ``graph.compile(checkpointer=...)`` 的 AsyncSqliteSaver。

    异常:
        RuntimeError: ``init_storage`` 尚未调用（路径提供方拿不到文件位置）。
    """

    async with AsyncSqliteSaver.from_conn_string(_engine_checkpoint_path()) as saver:
        yield saver
