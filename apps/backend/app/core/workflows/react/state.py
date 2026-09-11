"""ReAct-like 工作流的 graph state 定义。

本模块只承载交给 LangGraph 管理的 graph state 数据契约。state 是 graph 各节点之间传递的
唯一数据通道，由 LangGraph 在节点返回增量后自动合并并
经 ``AsyncSqliteSaver`` checkpointer 持久化（断点续跑与审批中断重放的依据）。
"""

from typing import Any

from pydantic import BaseModel

from app.core.workflows.nodes.helper.tool_call_lifecycle import ToolCallLifecycleManager


class ReactGraphState(BaseModel):
    """ReAct-like 工作流交给 LangGraph 管理的 graph state（节点间唯一数据通道）。

    节点只通过 ``return`` 返回增量、由框架合并，不跨节点直接持有彼此数据；模型消息与运行期
    ``runtime_context`` 均**不进 state**（归 ``RuntimeContextManager`` 与 config 注入）。
    以下所有字段均持久化进 checkpoint。每个字段标注「写入方 / 消费方」。

    Attributes:
        repair_requested: 是否需要修复重写。model 节点 REPAIR 回流置 True；条件边
            ``_should_continue`` 消费。
        step_count: 当前模型步骤序号（model 节点进入时 +1）。发起推理前按
            ``step_count > max_steps`` 拦截，超配额调用 ``_finalize_max_steps`` 收口终态。
        tool_error_count: 连续工具失败次数，成功即清零。observe 节点从本批
            ``last_tool_results`` 重算并消费（超 ``tool_error_limit`` 判定）。
        requested_tool: 当前步骤是否请求工具调用。model 节点写；``_should_continue``
            消费（决定走 tools 还是 END）。
        final_response: 当前步骤是否已产出最终回答。model 节点写；``_should_continue`` 消费。
        terminal: 是否进入完成/失败/取消等终止态。model / tools / observe 节点写；编排层
            结合 ``aget_state().tasks`` 判定结束。
        pending_tool_calls: 待执行的工具调用（可序列化 dict）。model 节点写，tools 节点经
            工具节点直接消费。dict 含 ``tool_name`` / ``arguments`` / ``call_id``，
            模型同时产出文本与工具调用时另带 ``instruction`` 键。
        deferred_repair_message: 本轮同时存在合法与可修复非法工具调用时，待工具结果
            全部写回上下文后追加的修复提示。model 节点写，observe 节点在
            ``ToolMessage`` 之后消费并清空，避免形成 ``AIMessage -> SystemMessage ->
            ToolMessage`` 的非法消息顺序。
        max_steps: 本轮允许的最大模型步骤数，执行期常量。编排层初始化；model 节点
            ``step_count > max_steps`` 判定用。
        final_text: 终态可见文本：正常完成为模型最终回答，步数耗尽由 ``_finalize_max_steps``
            写默认失败说明。
        last_tool_results: 本批工具结果摘要（可序列化 dict，由 tools 节点对本批
            ``ToolObservation`` 做 ``dataclasses.asdict`` 投影，键名即执行层字段名
            ``tool_call_id`` / ``display_data``）。tools 节点写；observe 节点做事件分发、
            错误计数与错误上限判定，其中 ``display_data`` 不会进入模型消息。
        tool_call_lifecycle: 当前 workflow 已创建工具调用的可序列化生命周期记录。model
            节点写入创建/运行状态，tools 节点写入执行前取消，observe 节点写入终态；不含
            operations、stream writer 或 runtime context。
        continuation_error_data: 终态排查用错误明细（可序列化 dict，通常含 ``error_kind`` /
            ``invalid_count``）。编排层初始化置 ``None``；model_node 在 REPAIR 工具分支写入
            脱敏计数、REPAIR 回流时置 ``None``；``_finalize_max_steps`` 消费并并入 ``RUN_FAILED``。
    """

    repair_requested: bool
    step_count: int
    tool_error_count: int
    requested_tool: bool
    final_response: bool
    terminal: bool
    pending_tool_calls: dict[str, Any]
    max_steps: int
    final_text: str
    last_tool_results: dict[str, Any]
    tool_call_lifecycle: ToolCallLifecycleManager | None = None
    deferred_repair_message: str = ""
    continuation_error_data: Any = None
