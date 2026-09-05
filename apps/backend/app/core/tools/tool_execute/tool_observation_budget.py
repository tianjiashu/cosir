"""工具观察的双通道输出预算：模型通道截断落盘 + 展示通道截断。

工具观察有两条互不替代的消费通道，各自需要独立预算：

- 模型通道 ``content``：受 ``ToolOutputBudget`` 约束（``Settings.MAX_TOOL_OUTPUT_CHARS``），
  超限截断并在 workspace 内落盘完整 artifact，同时做终端输出脱敏；
- 客户端展示通道 ``data``：受 ``DisplayDataBudget`` 约束（单字段 2000 字符），
  避免展示通道绕过模型通道预算无约束膨胀。

本模块是这两条预算的**唯一组合点**：执行管线在每条返回路径（成功、失败、提前返回）
都经本类统一治理，避免「某条分支漏过预算」导致未脱敏或未截断的输出进入模型上下文、
事件流或 checkpoint。
"""

from app.core.tools.guard.display_data_budget import DisplayDataBudget
from app.core.tools.guard.tool_output_budget import ToolOutputBudget
from app.core.tools.schemas import ToolExecutionContext, ToolObservation


class ToolObservationBudget:
    """对工具观察的两条消费通道统一应用字符预算。"""

    def __init__(
        self,
        output_budget: ToolOutputBudget | None = None,
        display_data_budget: DisplayDataBudget | None = None,
    ) -> None:
        """初始化双通道预算。

        参数:
            output_budget: 模型可见 ``content`` 的预算；缺省使用
                ``ToolOutputBudget`` 自带默认值。
            display_data_budget: 客户端展示数据通道的预算；缺省使用
                ``DisplayDataBudget`` 自带默认值。

        返回:
            无。

        异常:
            ValueError: 任一预算的 ``max_chars`` 小于 1 时抛出（由预算类自己校验）。

        副作用:
            持有两个预算实例，不触发任何工具执行。
        """

        self._output_budget = output_budget or ToolOutputBudget()
        self._display_data_budget = display_data_budget or DisplayDataBudget()

    def apply(
        self,
        observation: ToolObservation,
        execution_context: ToolExecutionContext | None,
    ) -> ToolObservation:
        """对任意成功、失败或提前返回观察统一应用输出预算，超出预算截断，并保留本地文件。

        先治理模型通道（脱敏 + 截断 + artifact spill），再治理展示通道（长文本截断），
        顺序固定：展示通道的截断标记依赖模型通道已在 ``data`` 中写入的治理字段。

        参数:
            observation: 待返回给上层的工具观察。
            execution_context: 当前 workspace 上下文；为 None 时 artifact 不落盘，
                退化为纯截断。

        返回:
            已脱敏并受两条通道字符预算约束的观察。

        异常:
            无。artifact 写入失败由 :class:`ToolOutputBudget` 内部退化处理。

        副作用:
            超限且有 workspace 时可能写入脱敏 artifact。
        """

        budgeted = self._output_budget.apply(observation, execution_context)
        return self._display_data_budget.apply(budgeted)
