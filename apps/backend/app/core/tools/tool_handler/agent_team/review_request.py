"""Agent Team「执行前确认」请求的构造（human-in-the-loop 请求侧的 Agent Team 实现）。

本模块只负责把 preparation 服务产出的预览字段投影成一份 :class:`UserInputRequest`：谁被问
（``team_run_id`` 作为 ``request_id``）、允许哪些决定、以及供用户编辑的目标与子目标草稿。
请求由 ``agent_team`` 工具随观察下发（``ToolObservation.user_input_request``），挂起、投影与
决定消费都在工作流侧，本模块不参与。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.core.tools.schemas import UserDecisionKind, UserInputRequest

# 请求种类：框架不解释其含义，只透传前端；前端按它选择渲染器。
AGENT_TEAM_REVIEW_KIND = "agent_team_review"

# 前端按本标识选择「可编辑运行输入」的渲染器；修改草稿形状时必须同步升版本，
# 避免旧前端把新草稿按旧结构渲染。
AGENT_TEAM_REVIEW_DRAFT_SCHEMA = "agent-team-review-v1"

_ACCEPTED_DECISIONS = (UserDecisionKind.APPROVE, UserDecisionKind.REJECT)


def build_team_review_request(
    preview_fields: Mapping[str, Any],
    *,
    team_run_id: int,
) -> UserInputRequest:
    """构造 Agent Team 的「确认执行方案」请求。

    参数:
        preview_fields: preparation 服务产出的预览字段（含 ``goal`` / ``node_goals`` /
            ``configuration``）。
        team_run_id: 待确认的 TeamRun 主键，直接作为 ``request_id``（同一 Run 内由自增主键保证
            唯一；工具未来换内部标识时必须保持本字段对前端稳定）。

    返回:
        待用户批准 / 驳回的请求；草稿是用户可直接编辑的目标与子目标。

    异常:
        KeyError: ``preview_fields`` 缺少草稿所需字段——准备服务的输出契约已经违反，必须上抛
            而不是让待确认卡片少掉用户要编辑的内容。

    副作用:
        无（纯函数）。
    """

    return UserInputRequest(
        request_id=str(team_run_id),
        kind=AGENT_TEAM_REVIEW_KIND,
        prompt="确认执行方案",
        decisions=_ACCEPTED_DECISIONS,
        draft_schema=AGENT_TEAM_REVIEW_DRAFT_SCHEMA,
        draft={
            "goal": preview_fields["goal"],
            "node_goals": preview_fields["node_goals"],
            "configuration": preview_fields["configuration"],
        },
    )
