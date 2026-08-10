"""ReAct 工作流的运行时配置注入容器。

本模块只承载 ReAct 工作流节点共享的运行时依赖注入容器 ``RuntimeConfig``，不依赖任何
节点、边或编排逻辑。它对应 LangGraph 运行上下文 ``config["configurable"]["runtime_config"]``
中的运行时依赖，与 ``ReactGraphState``（节点间数据流）职责分离：

- state 是持久化、累积、节点之间传递的**数据流**，由 LangGraph 写入 checkpoint。
- ``RuntimeConfig`` 是**不进入 checkpoint** 的运行期依赖（含不可序列化的模型实例与操作门面），
  由编排层在 ``ReactLikeWorkflow.run()`` 时注入，节点经 ``get_config()`` 取出后读取字段。

``RuntimeConfig`` 用 ``@dataclass`` 表达：它不是数据契约（那是 ``ReactGraphState`` 的 pydantic
职责），而是依赖集合；其字段含不可 JSON 序列化的对象（``BaseChatModel`` / ``RuntimeOperations``），
因此不纳入 checkpoint 持久化，graph 重放时由编排层重新注入。
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from langchain_core.language_models import BaseChatModel

from app.core.runtime.runtime_operations import RuntimeOperations
from app.models import TaskRecord, TurnRecord
from app.models.turn_usage_stats import TurnUsageStats
from app.tools.schemas import ToolCall


@dataclass
class RuntimeConfig:
    """ReAct 工作流节点共享的运行时依赖注入容器。

    编排层在 ``ReactLikeWorkflow.run()`` 中把本对象放入
    ``config["configurable"]["runtime_config"]``，各节点通过
    ``get_config()["configurable"]["runtime_config"]`` 取出后读取字段，避免在多个节点里用裸
    字符串 key 从 ``config["configurable"]`` 取值。

    与 ``ReactGraphState`` 的区别：
    - state（graph state）：节点之间传递的**数据流**，会被 LangGraph 持久化进 checkpoint、
      随执行累积（如 messages、step_count），是可重放的。
    - 本容器：运行期**依赖注入**，含不可序列化对象（模型实例、操作门面），**不进入 checkpoint**，
      只在本次 graph 执行期间生效，graph 重放时由编排层重新注入。

    Attributes:
        operations: 运行时操作门面，提供模型调用、工具执行、事件记录与状态更新能力。
        task: 当前需要执行的任务记录。
        turn: 当前执行轮次记录，节点经它写入 turn 状态（单一事实来源）。
        model: 已绑定工具的 LangChain chat model 实例，供 model 节点推理。
        approval_resolver: 可选的工具审批解析器；``tools`` 节点因 ``interrupt()`` 暂停时，
            用它把待审批的工具调用解析为「批准执行的调用列表」。``None`` 表示自动放行全部调用，
            且 ``tools`` 节点不调用 ``interrupt()``（不暂停 graph），直接执行工具。
        start_time: graph 开始执行的 ``time.perf_counter()`` 时间戳，用于在 ``run_finished``
            中计算耗时。
        usage_stats: turn 级 token 与耗时累加器；model 节点在每次模型调用后把
            ``usage_metadata`` 累加进来，``run_finished`` 事件读取后下发给前端。
        langfuse_trace_id: 本 turn 的 Langfuse trace 标识；由 runner 在启用 tracing 时注入，
            供终态事件 payload 携带给前端展示。未启用 Langfuse 时为 None。
    """

    operations: RuntimeOperations
    turn: TurnRecord
    model: BaseChatModel
    # None 表示自动放行全部调用，且 tools 节点不调用 interrupt()（不暂停 graph）。
    approval_resolver: Callable[[list[ToolCall]], list[ToolCall]] | None = None
    start_time: float = 0.0
    usage_stats: TurnUsageStats = field(default_factory=TurnUsageStats)
    langfuse_trace_id: str | None = None
