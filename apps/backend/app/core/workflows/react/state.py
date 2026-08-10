"""ReAct-like 工作流的 graph state 定义。

本模块只承载交给 LangGraph 管理的 graph state 数据契约与追加式 reducer，不依赖任何
节点、边或编排逻辑。state 是 graph 各节点之间传递的唯一数据通道。
"""

from typing import Any

from pydantic import BaseModel


class ReactGraphState(BaseModel):
    """ReAct-like 工作流交给 LangGraph 管理的 graph state（节点间唯一数据通道）。

    本 state 是 graph 各节点之间传递的**数据流**，与 ``RuntimeConfig``（运行期依赖注入、
    不持久化）职责严格分离：

    - **持久化**：state 由 LangGraph 在每次节点返回增量后自动合并，并由 ``AsyncSqliteSaver``
      checkpointer 持久化进 SQLite；graph 因 ``interrupt()`` 暂停或进程崩溃后可从 checkpoint
      重放恢复。
    - **累积**：模型上下文由 ``RuntimeContext`` 独占管理，工具观察结果经
      ``_runtime_context().add_message()`` 追加到上下文末端（**不进 graph state**）。
    - **单一事实来源**：节点只通过 ``return`` 返回增量、由框架合并；节点永不跨节点直接
      持有彼此数据。
    - **运行期上下文**：``runtime_context`` 不进入 graph state（非 list 对象不兼容 state
      reducer），由编排层经 ``config["configurable"]["runtime_context"]`` 注入，节点通过
      ``_runtime_context()`` 读取。
    - **消息不进 state**：模型推理输入恒来自 ``_runtime_context().load_message()``，节点
      **不得**返回 ``messages`` 字段；消息持久化事实来源是 SQLite（节点经
      ``append_runtime_message`` 逐条落库），checkpoint 只承载控制流状态。错误恢复时由
      ``RuntimeContext.load_for_task`` 从 DB 重建上下文。

    每个字段的边界约定如下（写入方 = 哪个节点 ``return`` 该字段；消费方 = 谁读取它；
    是否持久化 = 是否进入 checkpoint）：

    Attributes:
        step_count: 已执行的模型步骤数，配合 ``max_steps`` 防无限循环。**写入方**：``model``
            节点（每步 +1）。**消费方**：``model`` 节点（超步数判定）、``tools`` 节点（生成
            step_id）。**持久化**：是。
        tool_error_count: 连续工具失败次数，成功即清零。**写入方**：``observe`` 节点（从本批
            ``last_tool_results`` 重算）。**消费方**：``observe`` 节点（超 ``tool_error_limit``
            判定）。**持久化**：是。
        requested_tool: 当前模型步骤是否请求工具调用。**写入方**：``model`` 节点。**消费方**：
            条件边 ``_should_continue``（决定走 tools 还是 END）。**持久化**：是。
        final_response: 当前模型步骤是否已产出最终回答。**写入方**：``model`` 节点。**消费方**：
            条件边 ``_should_continue``。**持久化**：是。
        terminal: 工作流是否进入完成/失败/取消等终止态。**写入方**：``model`` / ``tools`` /
            ``observe`` 节点。**消费方**：编排层 ``while True`` 循环（结合 ``aget_state().tasks``
            判定是否真结束）。**持久化**：是。
        pending_tool_calls: 模型请求、待执行的工具调用（可序列化 dict 列表，由 ``model`` 节点
            写入，``tools`` 节点经 ``interrupt()`` 暂停审批后消费）。每个 dict 固定含
            ``tool_name`` / ``arguments`` / ``call_id`` 三键；当模型在本轮**同时**产出文本与
            工具调用时，还会额外带上 ``instruction`` 键（模型调工具前的说明文本），供
            ``tools`` / ``observe`` 节点在日志与错误排查时看到模型意图。``instruction`` 缺省
            视为空串，向后兼容无文本的同批调用。**持久化**：是。
        max_steps: 本轮允许的最大模型步骤数，执行期常量。**写入方**：编排层初始化 input_state。
            **消费方**：``model`` 节点（超步数判定）。**持久化**：是。
        final_text: 模型产出的最终回答文本，终态时落库，并在 checkpoint 重放时用于恢复，避免
            重放丢失最终回复。**写入方**：``model`` 节点（终态）。**消费方**：编排层/客户端。
            **持久化**：是。
        last_tool_results: ``tools`` 节点执行后的本批工具结果摘要（可序列化 dict 列表），供
            ``observe`` 节点做错误计数与错误上限判定、并为后续「LLM 观察工具结果」提供原材料。
            **不承载** ``data`` 等大体积结构化字段，``content`` 已截断到安全长度以防撑爆
            checkpoint。**写入方**：``tools`` 节点。**消费方**：``observe`` 节点。**持久化**：是。
    """

    step_count: int
    tool_error_count: int
    requested_tool: bool
    final_response: bool
    terminal: bool
    pending_tool_calls: list[dict[str, Any]]
    max_steps: int
    final_text: str
    # 本批工具执行结果摘要（可序列化），供 observe 节点判定与后续 LLM 观察使用。
    last_tool_results: list[dict[str, Any]]
