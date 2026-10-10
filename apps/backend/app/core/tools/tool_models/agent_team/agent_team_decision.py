"""Agent Team 工具在 human-in-the-loop 流程中消费的用户输入形状。

用户在预览卡上编辑目标与子目标后再批准，因此「批准」携带一份完整运行输入；本模块只定义其
形状与必填约束，配置的领域校验仍由 ``AgentTeamRunService`` 在确认边界完成（不在工具层重复）。

驳回不需要本模块：驳回不执行工具，用户意见由 ``wait_user`` 节点内联进该调用的观察（模型据此
重新提案），因此没有需要在此校验的业务字段。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from app.agent_team.configuration.team_node_definition import TeamNodeIdentifier


class AgentTeamApproveInput(BaseModel):
    """用户批准执行方案时提交的最终运行输入。

    与 ``AgentTeamRunService.confirm_and_start`` 的确认参数一一对应；``configuration`` 保持为
    原始文档（不在此解析为领域模型），避免工具层与确认边界两处各自做一遍配置校验。
    """

    model_config = ConfigDict(extra="forbid")

    configuration: dict[str, Any]
    goal: StrictStr = Field(min_length=1, max_length=50_000)
    node_goals: dict[TeamNodeIdentifier, StrictStr] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_run_inputs(self) -> AgentTeamApproveInput:
        """在用户确认输入边界拒绝空白目标与保留节点标识。"""

        if not self.goal.strip():
            raise ValueError("goal must not be blank")
        if any(not value.strip() for value in self.node_goals.values()):
            raise ValueError("node_goals values must not be blank")
        if "END" in self.node_goals:
            raise ValueError("END is a reserved terminal target, not a Team node")
        return self


__all__ = ["AgentTeamApproveInput"]
