"""ReAct 工作流的运行时依赖注入容器。

承载节点共享的运行期依赖 ``RuntimeConfig``，不依赖任何节点、边或编排逻辑。它注入到
``config["configurable"]["runtime_config"]``，与 ``ReactGraphState``（持久化数据流）职责分离：
state 随执行累积并写入 checkpoint；``RuntimeConfig`` 含不可序列化对象（模型实例、操作门面），
**不进入 checkpoint**，由编排层在 ``ReactLikeWorkflow.run()`` 时注入、graph 重放时重新注入。
"""

from dataclasses import dataclass, field

from langchain_core.runnables import Runnable

from app.core.agents.structured_output_spec import StructuredOutputSpec
from app.core.runtime.execution_mode import ExecutionMode
from app.core.workflows.conversation_run_usage_stats import ConversationRunUsageStats
from app.core.workflows.workflow_operations import WorkflowOperations
from app.models import ConversationRunRecord


@dataclass
class RuntimeConfig:
    """ReAct 工作流节点共享的运行时依赖注入容器。

    编排层在 ``ReactLikeWorkflow.run()`` 中把本对象放入
    ``config["configurable"]["runtime_config"]``，各节点取出后读取字段，避免在多个节点里用裸
    字符串 key 从 ``config["configurable"]`` 取值。本容器含不可序列化对象，不进入 checkpoint，
    仅本次 graph 执行期间有效，重放时由编排层重新注入。

    Attributes:
        operations: 运行时操作门面，提供模型调用、工具执行、事件记录与状态更新能力。
        run: 当前执行的 Conversation Run 记录，节点经它写入 run 状态（单一事实来源）。
        model: 根据 Task 冻结工具 schema 绑定的 Runnable；无工具时为基础模型。结构化输出节点
            在同一模型上临时绑定严格 JSON Schema response_format，保留请求中的工具 schema。
        structured_output: 本次 Agent Run 冻结的可选最终 JSON Schema 契约。
        start_time: graph 开始执行的 ``time.perf_counter()`` 时间戳（由 ``ReactLikeWorkflow.run``
            写入）。**当前无读取方**：耗时统计不在 workflow 内计算，字段保留待用。
        usage_stats: run 级 token 累加器；model 节点在每次模型调用后把 ``usage_metadata``
            累加进来，run 终态事件读取后下发给前端。
        langfuse_trace_id: 本 run 的 Langfuse trace 标识；由 runner 在启用 tracing 时注入。
            **当前 workflow 节点不读取该字段**（终态事件不再由 workflow 生产）。
        thinking_channel: 统一 OpenAI-compatible reasoning 输出字段；模型不支持思考时为空串。
        execution_mode: 本次 graph 是新运行（``fresh``）还是从既有 checkpoint 恢复
            （``resume`` / ``resume_with_input``），并据此决定是否清空该 run 的旧上下文条目
            （``fresh`` 按 run 清、续跑保留，见 ``RuntimeContextManager.begin_run``）。工作流
            据此选择传入 graph 的输入：``fresh`` 传初始 state；``resume`` 传
            ``Command(goto="model")`` 回退重跑；``resume_with_input`` 在恢复现场存在 interrupt
            时传 ``Command(resume=...)`` 喂入用户决定，否则传 ``None`` 让 LangGraph 从既有
            checkpoint 继续（因此续跑**不可**换线程）。工具节点不依赖本字段判断重放，重放由
            工具调用自身的生命周期状态决定（见 ``tools_node``）。
        resuming_wait_user: 当前输入是否正在恢复 ``wait_user`` 节点（human-in-the-loop）
            的 interrupt。只有该节点的 interrupt 恢复重放时为 True；续跑后首次到达等待节点
            仍为 False。**一次性消费令牌**：``wait_user`` 消费掉恢复值后立即复位为 False，
            使同一 graph 执行内由自环再次进入该节点时不会被误判为「正在消费恢复值」（否则会
            跳过 Run 状态迁移，让数据库停在 ``running`` 而图已挂起）。
    """

    operations: WorkflowOperations
    run: ConversationRunRecord
    model: Runnable
    structured_output: StructuredOutputSpec | None = None
    start_time: float = 0.0
    usage_stats: ConversationRunUsageStats = field(default_factory=ConversationRunUsageStats)
    langfuse_trace_id: str | None = None
    thinking_channel: str = ""
    execution_mode: ExecutionMode = "fresh"
    resuming_wait_user: bool = False
