from typing import Any

from pydantic import BaseModel


class AgentProfileResponse(BaseModel):
    """校验并序列化单个已注册 agent 的元信息。

    字段集与 ``AgentProfile.to_dict()`` 完全一致，供前端渲染 agent 选择器与说明；
    二者任一改动都必须同步（见分层约束）。``goal`` 字段已由 ``description`` 取代，
    Agent 的职责、能力、适用场景与约束统一收敛到 ``description`` 文本。

    参数:
        agent_id: 稳定的 Agent 标识。
        role: 人类可读的 Agent 角色。
        description: 注入到模型上下文中的运行目标与职责描述（含能力/场景/约束，替代旧 ``goal``）。
        allowed_tools: 该 Agent 允许使用的工具名或权限名。
        workflow: 该 Agent 使用的执行策略标识。
        model_name: 该 Agent 使用的模型名称。
        max_steps: 单轮最大执行步数。
        prompt_ref: 关联的 prompt 引用字典（第二部分接缝，可为 None）。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    agent_id: str
    role: str
    description: str
    allowed_tools: list[str]
    workflow: str
    # 阶段 1.5 后允许 None：5 个内置 profile 不内置默认模型，前端据此
    # 区分「未配置」与「已配置」并强制用户选择。
    model_name: str | None
    max_steps: int
    prompt_ref: dict[str, Any] | None
