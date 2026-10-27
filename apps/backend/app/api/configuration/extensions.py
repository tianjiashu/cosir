"""系统级 MCP 与 Skill 配置路由。"""

from __future__ import annotations

from app.api.configuration.errors import raise_configuration_error
from app.api.schemas.request.MCPServersUpdateRequest import MCPServersUpdateRequest
from app.api.schemas.request.MCPServerTestRequest import MCPServerTestRequest
from app.api.schemas.request.SkillsEnabledUpdateRequest import SkillsEnabledUpdateRequest
from app.api.schemas.request.SkillsImportRequest import SkillsImportRequest
from app.api.schemas.response.MCPServersResponse import (
    MCPServerConfigResponse,
    MCPServersResponse,
)
from app.api.schemas.response.MCPServerTestResponse import MCPServerTestResponse
from app.api.schemas.response.SkillDeleteResponse import SkillDeleteResponse
from app.api.schemas.response.SkillDocumentResponse import SkillDocumentResponse
from app.api.schemas.response.SkillInfoResponse import SkillInfoResponse
from app.app import app
from app.service.configuration.mcp_configuration_service import MCPConfigurationService
from app.service.configuration.skill_configuration_service import SkillConfigurationService
from app.core.tool.handler.mcp.mcp_client import (
    MCPConfiguration,
    MCPServerConfig,
    server_connection_fingerprint,
)
from app.core.tool.handler.skill.skill_catalog import SkillInfo


@app.get("/configuration/mcp-servers", response_model=MCPServersResponse)
async def get_mcp_servers_configuration() -> MCPServersResponse:
    """读取系统级 MCP 服务配置。"""

    try:
        configuration = MCPConfigurationService().read()
        return _mcp_servers_response(configuration)
    except Exception as exc:
        raise_configuration_error(exc)


@app.put("/configuration/mcp-servers", response_model=MCPServersResponse)
async def update_mcp_servers_configuration(
    payload: MCPServersUpdateRequest,
) -> MCPServersResponse:
    """替换并保存系统级 MCP 服务配置。"""

    try:
        servers = [
            MCPServerConfig.model_validate(server.model_dump()) for server in payload.servers
        ]
        configuration = MCPConfigurationService().update(MCPConfiguration(servers=servers))
        return _mcp_servers_response(configuration)
    except Exception as exc:
        raise_configuration_error(exc)


@app.post("/configuration/mcp-servers/test", response_model=MCPServerTestResponse)
async def test_mcp_server_configuration(
    payload: MCPServerTestRequest,
) -> MCPServerTestResponse:
    """短连接测试未保存的 MCP 配置并读取工具目录。"""

    server = MCPServerConfig.model_validate(payload.model_dump())
    result = await MCPConfigurationService().test(server)
    return MCPServerTestResponse(
        success=result.success,
        tools=result.tools,
        error=result.error,
        agent_available=result.agent_available,
    )


@app.get("/configuration/skills", response_model=list[SkillInfoResponse])
async def list_system_skills() -> list[SkillInfoResponse]:
    """列出系统 Skill 目录中的有效技能及启用状态。"""

    try:
        return [_skill_response(skill) for skill in SkillConfigurationService().list()]
    except Exception as exc:
        raise_configuration_error(exc)


@app.get("/configuration/skills/{name}", response_model=SkillDocumentResponse)
async def get_system_skill(name: str) -> SkillDocumentResponse:
    """读取只读详情，不受 Skill 当前启用状态限制。"""

    try:
        document = SkillConfigurationService().read(name)
        return SkillDocumentResponse(
            name=document.info.name,
            description=document.info.description,
            enabled=document.info.enabled,
            content=document.content,
        )
    except Exception as exc:
        raise_configuration_error(exc)


@app.put("/configuration/skills/enabled", response_model=list[SkillInfoResponse])
async def update_system_skill_enabled(
    payload: SkillsEnabledUpdateRequest,
) -> list[SkillInfoResponse]:
    """保存系统 Skill 启用名单。"""

    try:
        skills = SkillConfigurationService().update_enabled(payload.enabled_skills)
        return [_skill_response(skill) for skill in skills]
    except Exception as exc:
        raise_configuration_error(exc)


def _skill_response(skill: SkillInfo) -> SkillInfoResponse:
    """把目录型领域对象投影为不暴露本机路径的 HTTP 响应。"""

    return SkillInfoResponse(name=skill.name, description=skill.description, enabled=skill.enabled)


def _mcp_servers_response(configuration: MCPConfiguration) -> MCPServersResponse:
    """把核心 MCP 配置映射为稳定的 HTTP 响应结构。"""

    return MCPServersResponse(
        servers=[
            MCPServerConfigResponse.model_validate(
                {
                    **server.model_dump(),
                    "tested": configuration.tested_servers.get(server.name)
                    == server_connection_fingerprint(server),
                }
            )
            for server in configuration.servers
        ]
    )


@app.post("/configuration/skills/import", response_model=SkillInfoResponse, status_code=201)
def import_system_skill(payload: SkillsImportRequest) -> SkillInfoResponse:
    """从桌面原生选择器选中的本机路径导入 Skill。"""

    try:
        service = SkillConfigurationService()
        if payload.source_type == "directory":
            skill = service.import_directory(payload.source_path)
        else:
            skill = service.import_archive(payload.source_path)
        return SkillInfoResponse(
            name=skill.name,
            description=skill.description,
            enabled=skill.enabled,
        )
    except Exception as exc:
        raise_configuration_error(exc)


@app.delete("/configuration/skills/{name}", response_model=SkillDeleteResponse)
async def delete_system_skill(name: str) -> SkillDeleteResponse:
    """删除系统目录内的 Skill。"""

    try:
        SkillConfigurationService().delete(name)
        return SkillDeleteResponse(name=name, deleted=True)
    except Exception as exc:
        raise_configuration_error(exc)
