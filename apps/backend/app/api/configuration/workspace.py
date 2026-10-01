"""workspace 基本配置 HTTP 路由。

本模块只负责 workspace ID、请求 schema 和响应 schema 的转换；文件安全、内容校验、Agent
Registry 同步及提示词变更广播分别由 configuration service 负责。
"""

from __future__ import annotations

from typing import Any

from app.api.configuration.errors import raise_configuration_error
from app.api.schemas.AgentConfigurationDocument import AgentConfigurationDocument
from app.api.schemas.request.AgentConfigurationRequest import AgentConfigurationRequest
from app.api.schemas.request.GlobalInstructionUpdateRequest import GlobalInstructionUpdateRequest
from app.api.schemas.response.AgentConfigurationResponse import AgentConfigurationResponse
from app.api.schemas.response.WorkspaceFileIgnoreResponse import WorkspaceFileIgnoreResponse
from app.api.schemas.response.WorkspaceInstructionResponse import WorkspaceInstructionResponse
from app.app import app
from app.service.configuration.agent_configuration_service import AgentConfigurationService
from app.service.configuration.workspace_configuration_context import (
    get_workspace_configuration_context,
)
from app.service.configuration.workspace_fileignore_configuration_service import (
    WorkspaceFileIgnoreConfigurationService,
)
from app.service.configuration.workspace_instruction_configuration_service import (
    WorkspaceInstructionConfigurationService,
)
from app.task_runtime.broadcaster.system_prompt_update_broadcaster import (
    broadcast_system_prompt_delta,
    build_system_prompt_delta,
)
from app.task_runtime.system_prompt_delta_source import SystemPromptDeltaSource


def _agent_service(workspace_id: int) -> AgentConfigurationService:
    """按 workspace ID 装配 workspace 作用域 Agent service。"""

    context = get_workspace_configuration_context(workspace_id)
    return AgentConfigurationService(workspace_root=context.root)


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
            AgentConfigurationResponse.from_document(item)
            for item in _agent_service(workspace_id).list_documents()
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
        document = AgentConfigurationDocument.from_payload(payload)
        saved = _agent_service(workspace_id).create_document(document)
        return AgentConfigurationResponse.from_document(saved)
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
        document = AgentConfigurationDocument.from_payload(payload)
        saved = _agent_service(workspace_id).update_document(agent_id, document)
        return AgentConfigurationResponse.from_document(saved)
    except Exception as exc:
        raise_configuration_error(exc)


@app.delete("/workspaces/{workspace_id}/configuration/agents/{agent_id}")
async def delete_workspace_configuration_agent(workspace_id: int, agent_id: str) -> dict[str, Any]:
    """删除 workspace 本地子 Agent。"""

    try:
        _agent_service(workspace_id).delete_document(agent_id)
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
        context = get_workspace_configuration_context(workspace_id)
        document = WorkspaceInstructionConfigurationService(context.root).read()
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
        context = get_workspace_configuration_context(workspace_id)
        service = WorkspaceInstructionConfigurationService(context.root)
        previous = service.read().content
        document = service.update(payload.content)
        delta = build_system_prompt_delta(
            source=SystemPromptDeltaSource.WORKSPACE_INSTRUCTIONS,
            previous=previous,
            current=document.content,
            scope=str(context.root),
        )
        if delta is not None:
            broadcast_system_prompt_delta(delta)
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
        context = get_workspace_configuration_context(workspace_id)
        document = WorkspaceFileIgnoreConfigurationService(context.root).read()
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
        context = get_workspace_configuration_context(workspace_id)
        document = WorkspaceFileIgnoreConfigurationService(context.root).update(payload.content)
        return WorkspaceFileIgnoreResponse.from_document(document)
    except Exception as exc:
        raise_configuration_error(exc)
