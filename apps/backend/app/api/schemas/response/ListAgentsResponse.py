from pydantic import BaseModel

from app.api.schemas.response.AgentProfileResponse import AgentProfileResponse


class ListAgentsResponse(BaseModel):
    """校验并序列化 agent 目录列表响应。

    参数:
        agents: 当前所有已注册 agent 的元信息列表。
        default_agent_id: 前端未显式选择时回落到的默认 agent 标识。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    agents: list[AgentProfileResponse]
    default_agent_id: str
