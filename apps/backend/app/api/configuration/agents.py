"""系统子 Agent 配置的 HTTP 路由与工具组投影。

负责 ``/configuration/agents`` 的增删改查，以及 API 层「工具组 ↔ 工具名」的双向投影：请求携带
工具组名称，落盘与注册的 Agent 文档携带工具名，两个方向的换算都在本模块完成，并按主 Agent 的
工具能力目录校验工具组是否已知。

不负责：其它子域路由（见同包 ``global_instructions`` / ``environment``）、异常到 HTTP 错误的
映射（见同包 ``errors``）、Agent 文件与注册表的读写校验（由 ``AgentConfigurationService``
承担）。请求与响应 schema 位于 ``app.api.schemas.request`` / ``app.api.schemas.response``。
"""

from __future__ import annotations

from typing import Any

from app.api.configuration.errors import raise_configuration_error
from app.api.schemas.AgentConfigurationDocument import AgentConfigurationDocument
from app.api.schemas.request.AgentConfigurationRequest import AgentConfigurationRequest
from app.api.schemas.response.AgentConfigurationResponse import AgentConfigurationResponse
from app.app import app
from app.service.configuration.agent_configuration_service import (
    AgentConfigurationService,
)


@app.get("/configuration/agents", response_model=list[AgentConfigurationResponse])
async def list_configuration_agents() -> list[AgentConfigurationResponse]:
    try:
        return [
            AgentConfigurationResponse.from_document(item)
            for item in AgentConfigurationService().list_documents()
        ]
    except Exception as exc:
        raise_configuration_error(exc)


@app.post("/configuration/agents", response_model=AgentConfigurationResponse)
async def create_configuration_agent(
    payload: AgentConfigurationRequest,
) -> AgentConfigurationResponse:
    try:
        return AgentConfigurationResponse.from_document(
            AgentConfigurationService().create_document(AgentConfigurationDocument.from_payload(payload))
        )
    except Exception as exc:
        raise_configuration_error(exc)


@app.put("/configuration/agents/{agent_id}", response_model=AgentConfigurationResponse)
async def update_configuration_agent(
    agent_id: str, payload: AgentConfigurationRequest
) -> AgentConfigurationResponse:
    try:
        return AgentConfigurationResponse.from_document(
            AgentConfigurationService().update_document(
                agent_id,
                AgentConfigurationDocument.from_payload(payload),
            )
        )
    except Exception as exc:
        raise_configuration_error(exc)


@app.delete("/configuration/agents/{agent_id}")
async def delete_configuration_agent(agent_id: str) -> dict[str, Any]:
    try:
        AgentConfigurationService().delete_document(agent_id)
        return {"agent_id": agent_id, "deleted": True}
    except Exception as exc:
        raise_configuration_error(exc)
