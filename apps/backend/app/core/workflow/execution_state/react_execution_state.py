"""Run 的执行状态：驱动游标与持久执行进度是同一个对象，整体存进 ``Run.execution_data``。"""

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums.react_route import ReactRoute
from app.core.workflow.execution_state.tool_batch import ToolBatch


class ReactExecutionState(BaseModel):
    """ReAct 循环的执行状态；workflow 每过一个节点整体落库，不承载生命周期状态。

    存与用分开：整个状态都持久化，但每次驱动只从 ``prepare_input`` 进入（:meth:`for_new_drive`），
    上一次驱动停在哪个节点、批次里有什么都不延续。这样续跑与唤醒总从模型边界继续，不重放已执行
    的工具；挂起这件事由 Run 状态 ``suspended`` 表达，不靠批次里的观察。

    不冻结任何工具调用：工具调用在产生它的那一步闭合，挂起时 context 中没有未闭合调用。

    字段:
        step_count: 累计步骤号，跨驱动递增；流式 part 身份与步数预算都依赖它不回退。
        step_limit: 本次驱动的步数上限（驱动起点的步骤号 + Agent 的 ``max_steps``）。
        next_node: 当前驱动内的下一个节点。
        batch: 当前模型请求的工具批次，节点间交接；prepare_input 判定挂起后清空。
        title_requested: 本 Run 是否已启动过标题支线；跨驱动保留，唤醒、恢复不再重复触发。
    """

    model_config = ConfigDict(extra="forbid")

    step_count: int = Field(default=0, ge=0)
    step_limit: int = Field(default=0, ge=0)
    next_node: ReactRoute = ReactRoute.PREPARE_INPUT
    batch: ToolBatch = Field(default_factory=ToolBatch)
    title_requested: bool = False

    def for_new_drive(self, max_steps: int) -> "ReactExecutionState":
        """返回新驱动的起点：保留跨驱动事实，复位只在单次驱动内有效的字段。

        新增字段时在这里决定它是否跨驱动保留（``title_requested`` 保留：一个 Run 只起一次标题）。
        每次显式执行获得新的步数预算；批次必须清空：
        observe 写入观察后若 prepare_input 因取消停止，落库的批次仍带挂起请求，唤醒或恢复时不能
        据此再次挂起，也不能把上一批观察再结算一遍。
        """
        return self.model_copy(
            update={
                "step_limit": self.step_count + max_steps,
                "next_node": ReactRoute.PREPARE_INPUT,
                "batch": ToolBatch(),
            }
        )
