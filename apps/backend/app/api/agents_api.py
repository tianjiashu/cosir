"""Agent 目录 API 路由。

端点以模块级 ``@app.get`` 直接注册到 ``app.api.app.app`` 单例上，
运行时通过 ``Depends(get_agent_registry)`` 注入进程级 registry 单例，
与执行引擎共享同一份 agent 目录。
"""

from fastapi import Depends

from app.api.dependencies import get_agent_registry
from app.api.schemas import AgentProfileResponse, ListAgentsResponse
from app.app import app
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.define_agents import DEFAULT_AGENT_ID


@app.get("/agents")
async def list_agents(
    registry: AgentProfileRegistry = Depends(get_agent_registry),
) -> ListAgentsResponse:
    """返回当前所有已注册 agent 的元信息，供前端构建 agent 选择器。

    参数:
        registry: 通过依赖注入的进程级 agent registry 单例。

    返回:
        ``ListAgentsResponse``：含 ``agents``（agent 元信息列表）与
        ``default_agent_id``（默认选中项）。

    异常:
        RuntimeError: 当 registry 单例未初始化时由依赖注入抛出。

    副作用:
        无。
    """

    return ListAgentsResponse(
        agents=[AgentProfileResponse(**profile.to_dict()) for profile in registry.list()],
        default_agent_id=DEFAULT_AGENT_ID,
    )
