"""工具观察的模型输出预算：模型通道截断落盘，展示通道保持完整。

工具观察有两条互不替代的消费通道，各自需要独立预算：

- 模型通道 ``content``：受 ``ToolOutputBudget`` 约束（``Settings.MAX_TOOL_OUTPUT_CHARS``），
  超限截断并在 workspace 内落盘完整 artifact；工具内容保持原文；
客户端展示通道 ``display_data`` 仅用于前端渲染，不经过模型输出预算；其字段安全投影由
各展示构造器负责，内容预算通过现有守卫处理后由 Transport 返回前端。execute_terminal
的终端输出在模型通道、artifact 和展示通道都保持原文。
"""

from app.core.tools.guard.tool_output_budget import ToolOutputBudget
from app.core.tools.schemas import ToolExecutionContext, ToolObservation


class ToolObservationBudget:
    """只对模型可见的 ``content`` 应用输出预算。"""

    def __init__(
        self,
        output_budget: ToolOutputBudget | None = None,
    ) -> None:
        """初始化模型输出预算。

        参数:
            output_budget: 模型可见 ``content`` 的预算；缺省使用
                ``ToolOutputBudget`` 自带默认值。

        返回:
            无。

        异常:
            ValueError: 任一预算的 ``max_chars`` 小于 1 时抛出（由预算类自己校验）。

        副作用:
            持有模型输出预算实例，不触发任何工具执行。
        """

        self._output_budget = output_budget or ToolOutputBudget()

    def apply(
        self,
        observation: ToolObservation,
        execution_context: ToolExecutionContext | None,
    ) -> ToolObservation:
        """对任意成功、失败或提前返回观察的模型内容应用输出预算。

        ``display_data`` 仅用于前端展示，原样保留，不在此处截断。

        参数:
            observation: 待返回给上层的工具观察。
            execution_context: 当前 workspace 上下文；为 None 时 artifact 不落盘，
                退化为纯截断。

        返回:
            受模型内容预算约束的观察；工具内容保持原文。
            展示数据不在本预算器中修改。

        异常:
            无。artifact 写入失败由 :class:`ToolOutputBudget` 内部退化处理。

        副作用:
            超限且有 workspace 时可能写入 artifact；execute_terminal artifact 保存原始输出。
        """

        return self._output_budget.apply(observation, execution_context)
