"""Run 的附属事实（``runs.extra`` JSON 列）：不参与调度、也不需要按它查询的内容。

为什么是一个 JSON 列：上线后给表加列要在每台机器上迁移，往这里加一个带默认值的字段不用。
因此这里的字段一律带默认值、只增不改；要筛选或排序的事实仍开正式列，执行状态与发起配置
各有自己的列（``execution_data``、``options``），不搬进来。
"""

from pydantic import BaseModel, ConfigDict, Field


class RunStatusChange(BaseModel):
    """Run 到达某个状态的一次记录；来源状态不存，就是上一条的 ``status``。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    # UTC ISO 文本，与这次迁移写下的 ``runs.updated_at`` 是同一个值。
    at: str
    status: str
    end_reason: str | None = None


class RunExtra(BaseModel):
    """``runs.extra`` 的严格结构，不开放任意扩展键。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # 状态迁移历史，第一条是创建。只供会话观测页读：``RunModel.status`` 才是生命周期的事实源，
    # 调度、恢复与投影规则不得读这里。由 ``RunCrud`` 在建 Run 与条件更新命中时追加。
    status_history: list[RunStatusChange] = Field(default_factory=list)
