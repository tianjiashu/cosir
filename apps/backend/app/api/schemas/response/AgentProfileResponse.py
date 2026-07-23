from pydantic import BaseModel


class AgentProfileResponse(BaseModel):
    """校验并序列化单个已注册 agent 的元信息。

    字段与 ``AgentProfile.to_dict()`` 保持一致，供前端渲染 agent 选择器与说明。

    参数:
        agent_id: 稳定的 Agent 标识。
        role: 人类可读的 Agent 角色。
        goal: 注入到模型上下文中的运行目标。
        allowed_tools: 该 Agent 允许使用的工具名或权限名。
        context_policy: 该 Agent 的上下文处理策略名称。
        workflow: 该 Agent 使用的执行策略标识。
        model_name: 该 Agent 使用的模型名称。
        max_steps: 单轮最大执行步数。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    agent_id: str
    role: str
    goal: str
    allowed_tools: list[str]
    context_policy: str
    workflow: str
    model_name: str
    max_steps: int
