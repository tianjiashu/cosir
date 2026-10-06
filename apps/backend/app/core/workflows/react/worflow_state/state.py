"""ReAct-like 工作流的 graph state 定义。

本模块只承载交给 LangGraph 管理的 graph state 数据契约。state 是 graph 各节点之间传递的
唯一数据通道，由 LangGraph 在节点返回增量后自动合并并
经 ``AsyncSqliteSaver`` checkpointer 持久化（断点续跑与审批中断重放的依据）。
"""

from typing import Any

from pydantic import BaseModel, Field

from app.core.workflows.react.node_helper.tool_call_lifecycle import ToolCallLifecycleManager
from app.core.workflows.react.worflow_state.route import ReactRoute
from app.core.workflows.react.worflow_state.terminal_session_checkpoint import (
    TerminalSessionCheckpoint,
)


class ReactGraphState(BaseModel):
    """ReAct-like 工作流交给 LangGraph 管理的 graph state（节点间唯一数据通道）。

    节点只通过 ``return`` 返回增量、由框架合并，不跨节点直接持有彼此数据；模型消息与运行期
    ``runtime_context`` 均**不进 state**（归 ``RuntimeContextManager`` 与 config 注入）。
    以下所有字段均持久化进 checkpoint。每个字段标注「写入方 / 消费方」。

    Attributes:
        instruction: 模型伴随工具调用产出的文本说明，随 state 下传给 tools / observe 节点，
            使下游执行与错误排查能看到模型当时的意图；无工具调用时为空字符串。
        tool_request: model 节点交给 tools 节点的原始工具请求，包含解析成功与解析失败的调用；
            只在当前 model -> tools 交接期间使用，不含完整模型消息。
        step_count: 当前模型步骤序号（model 节点进入时 +1）。发起推理前按
            ``step_count > max_steps`` 时切换到一次无工具最终回答请求。
        tool_rejection_count: 连续出现无效或阻塞工具批次的次数；获准工具批次清零。
        final_answer_only: 是否进入有界的无工具最终回答阶段；该阶段不再回到 tools。
        tool_feedback: 混合工具批次的拒绝反馈；由 observe 在所有 ToolMessage 写入后追加。
        tool_error_count: 连续工具失败次数，成功即清零。observe 节点从本批
            ``last_tool_results`` 重算并消费（超 ``Constant.Workflow.TOOL_ERROR_LIMIT`` 判定）。
        next_node: 当前节点完成后的唯一动态路由目标。model 节点写入 ``tools``、``model``、
            ``structured_output`` 或 ``end``；tools 节点写入 ``model`` 或 ``observe``；
            observe 节点写入 ``model``、``agent_team_wait`` 或 ``end``。固定转移由 graph
            普通边表达。``end`` 仅表示
            LangGraph 控制流结束，不表示 Run 的业务状态。
        max_steps: 本轮允许的最大模型步骤数，执行期常量。编排层初始化；model 节点
            ``step_count > max_steps`` 判定用。
        final_text: 终态可见文本；正常完成或无工具最终回答时为模型回答。
        last_tool_results: 本批工具结果摘要（可序列化 dict，由 tools 节点对本批
            ``ToolObservation`` 做 ``dataclasses.asdict`` 投影，键名即执行层字段名
            ``tool_call_id`` / ``display_data``）。tools 节点写；observe 节点做事件分发、
            错误计数与错误上限判定，其中 ``display_data`` 不会进入模型消息。
        terminal_sessions: 当前 Run 创建的 terminal 元数据，键为 session id，值为
            ``TerminalSessionCheckpoint``。只保存由工具展示契约 allowlist 后的可序列化字段，
            不保存 worker、PTY、输出 ring buffer 或 subscriber。活终端的真实性仍由 backend
            进程内 ``TerminalSessionService`` registry 负责。
        tool_call_lifecycle: 当前 workflow 已创建工具调用的可序列化生命周期记录。model
            节点不处理生命周期；tools 节点分类、创建与启动调用，observe 节点写入执行结果终态；
            不含 operations、stream writer 或 runtime context。
    """

    step_count: int
    tool_error_count: int
    next_node: ReactRoute
    instruction: str = ""
    tool_request: dict[str, Any] = Field(default_factory=dict)
    max_steps: int
    final_text: str
    last_tool_results: dict[str, Any]
    tool_rejection_count: int = 0
    final_answer_only: bool = False
    tool_feedback: str = ""
    terminal_sessions: dict[str, TerminalSessionCheckpoint] = Field(default_factory=dict)
    tool_call_lifecycle: ToolCallLifecycleManager | None = None
