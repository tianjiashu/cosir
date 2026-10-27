"""用户在 Agent Team 审批卡片上批准时提交的最终运行输入。

用户在预览卡上编辑目标、节点完整子目标与摘要后再批准，因此「批准」携带一份完整运行输入；本模块
只定义其形状与必填约束，配置的领域校验由 ``AgentTeamRunApproval.confirm`` 在确认边界完成。驳回只带
反馈文本，不需要本模块。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from app.models.agent_team.configuration.team_node_definition import TeamNodeIdentifier
from app.core.tool.handler.agent_team.args.node_goal import AgentTeamNodeGoal


class AgentTeamApproveInput(BaseModel):
    """用户批准执行方案时提交的最终运行输入。

    与 ``AgentTeamRunApproval.confirm`` 的确认参数一一对应；``configuration`` 保持为原始文档
    （不在此解析为领域模型），避免输入边界与确认边界两处各自做一遍配置校验。
    """

    model_config = ConfigDict(extra="forbid")

    configuration: dict[str, Any]
    goal: StrictStr = Field(min_length=1, max_length=50_000)
    node_goals: dict[TeamNodeIdentifier, AgentTeamNodeGoal] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_run_inputs(self) -> AgentTeamApproveInput:
        """在用户确认输入边界拒绝空白目标与保留节点标识。"""

        if not self.goal.strip():
            raise ValueError("goal must not be blank")
        if "END" in self.node_goals:
            raise ValueError("END is a reserved terminal target, not a Team node")
        return self


__all__ = ["AgentTeamApproveInput"]
