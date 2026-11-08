"""workspace 基本配置 HTTP 路由。

本模块只负责 workspace ID、请求 schema 和响应 schema 的转换；文件安全、内容校验、Agent
Registry 同步及提示词变更广播分别由 configuration service 负责。
"""

from __future__ import annotations

from typing import Any

from app.api.configuration.errors import raise_configuration_error
from app.api.schemas.request.AgentConfigurationRequest import AgentConfigurationRequest
from app.api.schemas.request.GlobalInstructionUpdateRequest import GlobalInstructionUpdateRequest
from app.api.schemas.response.AgentConfigurationResponse import AgentConfigurationResponse
from app.api.schemas.response.WorkspaceFileIgnoreResponse import WorkspaceFileIgnoreResponse
from app.api.schemas.response.WorkspaceInstructionResponse import WorkspaceInstructionResponse
from app.app import app
from app.service.configuration.agent_configuration_service import AgentConfigurationService
from app.service.configuration.workspace_configuration_root import resolve_workspace_root
from app.service.configuration.workspace_fileignore_configuration_service import (
    WorkspaceFileIgnoreConfigurationService,
)
from app.service.configuration.workspace_instruction_configuration_service import (
    WorkspaceInstructionConfigurationService,
)


def _agent_service(workspace_id: int) -> AgentConfigurationService:
    """按 workspace ID 装配 workspace 作用域 Agent service。"""

    root = resolve_workspace_root(workspace_id)
    return AgentConfigurationService(workspace_root=root)


@app.get(
    "/workspaces/{workspace_id}/configuration/agents",
    response_model=list[AgentConfigurationResponse],
)
async def list_workspace_configuration_agents(
    workspace_id: int,
) -> list[AgentConfigurationResponse]:
    """列出 workspace 本地子 Agent。"""

    try:
        return [
            AgentConfigurationResponse.from_entry(item)
            for item in _agent_service(workspace_id).list_entries()
        ]
    except Exception as exc:
        raise_configuration_error(exc)


@app.post(
    "/workspaces/{workspace_id}/configuration/agents",
    response_model=AgentConfigurationResponse,
)
async def create_workspace_configuration_agent(
    workspace_id: int,
    payload: AgentConfigurationRequest,
) -> AgentConfigurationResponse:
    """创建 workspace 本地子 Agent。"""

    try:
        saved = _agent_service(workspace_id).create(payload.to_configuration())
        return AgentConfigurationResponse.from_entry(saved)
    except Exception as exc:
        raise_configuration_error(exc)


@app.put(
    "/workspaces/{workspace_id}/configuration/agents/{agent_id}",
    response_model=AgentConfigurationResponse,
)
async def update_workspace_configuration_agent(
    workspace_id: int,
    agent_id: str,
    payload: AgentConfigurationRequest,
) -> AgentConfigurationResponse:
    """更新 workspace 本地子 Agent。"""

    try:
        saved = _agent_service(workspace_id).update(agent_id, payload.to_configuration())
        return AgentConfigurationResponse.from_entry(saved)
    except Exception as exc:
        raise_configuration_error(exc)


@app.delete("/workspaces/{workspace_id}/configuration/agents/{agent_id}")
async def delete_workspace_configuration_agent(workspace_id: int, agent_id: str) -> dict[str, Any]:
    """删除 workspace 本地子 Agent。"""

    try:
        _agent_service(workspace_id).delete(agent_id)
        return {"workspace_id": workspace_id, "agent_id": agent_id, "deleted": True}
    except Exception as exc:
        raise_configuration_error(exc)


@app.get(
    "/workspaces/{workspace_id}/configuration/instructions",
    response_model=WorkspaceInstructionResponse,
)
async def get_workspace_instruction_configuration(
    workspace_id: int,
) -> WorkspaceInstructionResponse:
    """读取 workspace 当前有效的 AGENTS.md。"""

    try:
        root = resolve_workspace_root(workspace_id)
        document = WorkspaceInstructionConfigurationService(root).read()
        return WorkspaceInstructionResponse.from_document(document)
    except Exception as exc:
        raise_configuration_error(exc)


@app.put(
    "/workspaces/{workspace_id}/configuration/instructions",
    response_model=WorkspaceInstructionResponse,
)
async def update_workspace_instruction_configuration(
    workspace_id: int,
    payload: GlobalInstructionUpdateRequest,
) -> WorkspaceInstructionResponse:
    """保存 workspace AGENTS.md，并通知当前进程中匹配的 workspace。"""

    try:
        root = resolve_workspace_root(workspace_id)
        document = WorkspaceInstructionConfigurationService(root).update(payload.content)
        return WorkspaceInstructionResponse.from_document(document)
    except Exception as exc:
        raise_configuration_error(exc)


@app.get(
    "/workspaces/{workspace_id}/configuration/fileignore",
    response_model=WorkspaceFileIgnoreResponse,
)
async def get_workspace_fileignore_configuration(workspace_id: int) -> WorkspaceFileIgnoreResponse:
    """读取 workspace .fileignore。"""

    try:
        root = resolve_workspace_root(workspace_id)
        document = WorkspaceFileIgnoreConfigurationService(root).read()
        return WorkspaceFileIgnoreResponse.from_document(document)
    except Exception as exc:
        raise_configuration_error(exc)


@app.put(
    "/workspaces/{workspace_id}/configuration/fileignore",
    response_model=WorkspaceFileIgnoreResponse,
)
async def update_workspace_fileignore_configuration(
    workspace_id: int,
    payload: GlobalInstructionUpdateRequest,
) -> WorkspaceFileIgnoreResponse:
    """校验并保存 workspace .fileignore。"""

    try:
        root = resolve_workspace_root(workspace_id)
        document = WorkspaceFileIgnoreConfigurationService(root).update(payload.content)
        return WorkspaceFileIgnoreResponse.from_document(document)
    except Exception as exc:
        raise_configuration_error(exc)
