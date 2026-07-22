"""ReAct-like 工作流的 graph state 定义。

本模块只承载交给 LangGraph 管理的 graph state 数据契约与追加式 reducer，不依赖任何
节点、边或编排逻辑。state 是 graph 各节点之间传递的唯一数据通道。
"""

from typing import Annotated, Any

from langchain_core.messages import BaseMessage
from pydantic import BaseModel


def _add_messages(
    existing: list[BaseMessage] | None, new: list[BaseMessage] | None
) -> list[BaseMessage]:
    """追加式合并 graph 消息通道。

    每个节点只返回本次新增的消息，reducer 负责把它们追加到已有上下文之后，
    供后续模型步骤继续推理。

    参数:
        existing: 通道中已存在的消息列表。
        new: 节点本次返回的新增消息列表。

    返回:
        合并后的完整消息列表。
    """

    return (existing or []) + (new or [])


class ReactGraphState(BaseModel):
    """ReAct-like 工作流交给 LangGraph 管理的 graph state（节点间唯一数据通道）。

    本 state 是 graph 各节点之间传递的**数据流**，与 ``RuntimeConfig``（运行期依赖注入、
    不持久化）职责严格分离：

    - **持久化**：state 由 LangGraph 在每次节点返回增量后自动合并，并由 ``AsyncSqliteSaver``
      checkpointer 持久化进 SQLite；graph 因 ``interrupt()`` 暂停或进程崩溃后可从 checkpoint
      重放恢复。
    - **累积**：消息通道使用追加式 reducer（``_add_messages``），工具观察结果会累加到上下文末端。
    - **单一事实来源**：节点只通过 ``return`` 返回增量、由框架合并；节点永不跨节点直接持有彼此数据。

    每个字段的边界约定如下（写入方 = 哪个节点 ``return`` 该字段；消费方 = 谁读取它；
    是否持久化 = 是否进入 checkpoint）：

    Attributes:
        messages: 模型上下文消息列表，追加式 reducer 累积。**写入方**：``model`` 节点（AI 消息）、
            ``tools`` 节点（工具观察消息）。**消费方**：``model`` 节点（作为推理输入）。**持久化**：是。
        step_count: 已执行的模型步骤数，配合 ``max_steps`` 防止无限循环。**写入方**：``model`` 节点
            （每步 +1）。**消费方**：``model`` 节点（判断是否超步数）、``tools`` 节点（生成 step_id）。
            **持久化**：是。
        tool_error_count: 连续工具执行失败次数，任意一次成功工具调用重置为 0。**写入方**：``tools`` 节点。
            **消费方**：``tools`` 节点（判断是否超过 ``tool_error_limit``）。**持久化**：是。
        requested_tool: 当前模型步骤是否请求了工具调用。**写入方**：``model`` 节点。**消费方**：
            条件边 ``_should_continue``（决定走 tools 还是 END）。**持久化**：是。
        final_response: 当前模型步骤是否已产出最终回答。**写入方**：``model`` 节点。**消费方**：
            条件边 ``_should_continue``。**持久化**：是。
        terminal: 工作流是否进入完成/失败/取消等终止态。**写入方**：``model`` / ``tools`` 节点。
            **消费方**：编排层 ``while True`` 循环（结合 ``aget_state().tasks`` 判定是否真结束）。
            **持久化**：是。
        pending_tool_calls: 模型请求、待执行的工具调用（可序列化 dict 列表，由 ``model`` 节点写入，
            ``tools`` 节点经 ``interrupt()`` 暂停审批后消费）。**持久化**：是。
        max_steps: 本轮允许的最大模型步骤数，执行期常量。**写入方**：编排层初始化 input_state。
            **消费方**：``model`` 节点（超步数判定）。**持久化**：是。
        final_text: 模型产出的最终回答文本，终态时落库，并在 checkpoint 重放时用于恢复，
            避免重放丢失最终回复。**写入方**：``model`` 节点（终态）。**消费方**：编排层/客户端。
            **持久化**：是。
    """

    messages: Annotated[list[BaseMessage], _add_messages]
    step_count: int
    tool_error_count: int
    requested_tool: bool
    final_response: bool
    terminal: bool
    pending_tool_calls: list[dict[str, Any]]
    max_steps: int
    final_text: str
