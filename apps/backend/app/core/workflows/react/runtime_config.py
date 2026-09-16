"""ReAct 工作流的运行时依赖注入容器。

承载节点共享的运行期依赖 ``RuntimeConfig``，不依赖任何节点、边或编排逻辑。它注入到
``config["configurable"]["runtime_config"]``，与 ``ReactGraphState``（持久化数据流）职责分离：
state 随执行累积并写入 checkpoint；``RuntimeConfig`` 含不可序列化对象（模型实例、操作门面），
**不进入 checkpoint**，由编排层在 ``ReactLikeWorkflow.run()`` 时注入、graph 重放时重新注入。
"""

from dataclasses import dataclass, field

from langchain_core.language_models import BaseChatModel

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
        model: 已绑定工具的 LangChain chat model 实例，供 model 节点推理。
        start_time: graph 开始执行的 ``time.perf_counter()`` 时间戳（由 ``ReactLikeWorkflow.run``
            写入）。**当前无读取方**：耗时统计不在 workflow 内计算，字段保留待用。
        usage_stats: run 级 token 累加器；model 节点在每次模型调用后把 ``usage_metadata``
            累加进来，run 终态事件读取后下发给前端。
        langfuse_trace_id: 本 run 的 Langfuse trace 标识；由 runner 在启用 tracing 时注入。
            **当前 workflow 节点不读取该字段**（终态事件不再由 workflow 生产）。
        thinking_channel: 按厂商分派的 thinking 抽取通道（如 ``"reasoning_content"`` /
            ``"thought"`` / ``"thinking_blocks"`` / ``"reasoning"``）；model 节点据此构造
            ``ModelChunkProcessor`` 选择 reasoning 抽取通道。空串表示不抽取独立 thinking 通道。
        thinking_roundtrip: 是否将 thinking 内容回传模型（多轮推理闭环）。当前
            ``ReactLikeWorkflow.run`` 固定传 ``True``，节点侧**尚未读取**，字段保留待用。
        vision_input_format: 按 provider 分派的视觉输入格式（如 ``"openai_url"``）；model 节点
            把它传给 ``resolve_messages_for_model`` 选择图片 block 的拼装方式。空串表示该
            provider 的格式尚未实现，真正要发图片时由 ``resolve_messages_for_model`` 抛
            ``VisionFormatNotSupportedError``。
        execution_mode: 本次 graph 是新运行（``fresh``）还是从既有 checkpoint 恢复
            （``resume``），并据此决定是否清空该 run 的旧上下文条目（``fresh`` 清、
            ``resume`` 保留，见 ``RuntimeContextManager.begin_run``）。工作流据此选择
            传入 graph 的输入：``fresh`` 传初始 state，``resume`` 传 ``None`` 表示从
            ``checkpoint_thread_id`` 指向线程的既有 checkpoint 继续（因此续跑**不可**
            换线程）。工具节点不依赖本字段判断重放，重放由工具调用自身的生命周期状态
            决定（见 ``tools_node``）。
    """

    operations: WorkflowOperations
    run: ConversationRunRecord
    model: BaseChatModel
    start_time: float = 0.0
    usage_stats: ConversationRunUsageStats = field(default_factory=ConversationRunUsageStats)
    langfuse_trace_id: str | None = None
    thinking_channel: str = ""
    thinking_roundtrip: bool = True
    vision_input_format: str = ""
    execution_mode: ExecutionMode = "fresh"
