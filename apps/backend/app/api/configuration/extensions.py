"""MCP 服务与 Skill 配置路由：系统级（``/configuration/...``）与 workspace 级
（``/workspaces/{workspace_id}/configuration/...``）。

两个作用域的路由形状一致，按能力放在同一个模块里；workspace 根目录只经 ``workspace_id`` 由
``resolve_workspace_root`` 解析，不接受客户端路径。列表返回本作用域看得到的合并结果（每项带
``scope`` / ``overrides``），写操作只作用于本作用域。
"""

from __future__ import annotations

from pathlib import Path

from app.api.configuration.errors import raise_configuration_error
from app.api.schemas.request.MCPServersUpdateRequest import MCPServersUpdateRequest
from app.api.schemas.request.MCPServerTestRequest import MCPServerTestRequest
from app.api.schemas.request.SkillEnabledUpdateRequest import SkillEnabledUpdateRequest
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
from app.service.configuration.mcp_configuration import MCPServerConfig, ScopedMCPServer
from app.service.configuration.mcp_configuration_service import MCPConfigurationService
from app.service.configuration.mcp_tested_servers import MCPTestedServers
from app.service.configuration.skill_catalog import SkillInfo
from app.service.configuration.skill_configuration_service import SkillConfigurationService
from app.service.configuration.workspace_configuration_root import resolve_workspace_root

_WORKSPACE = "/workspaces/{workspace_id}/configuration"


# ---------------------------------------------------------------------------
# MCP 服务
# ---------------------------------------------------------------------------


@app.get("/configuration/mcp-servers", response_model=MCPServersResponse)
async def get_system_mcp_servers() -> MCPServersResponse:
    """读取系统级 MCP 服务配置。"""

    return _list_mcp_servers(None)


@app.get(f"{_WORKSPACE}/mcp-servers", response_model=MCPServersResponse)
async def get_workspace_mcp_servers(workspace_id: int) -> MCPServersResponse:
    """读取工作区看得到的 MCP 服务（含继承的系统级服务）。"""

    return _list_mcp_servers(workspace_id)


@app.put("/configuration/mcp-servers", response_model=MCPServersResponse)
async def update_system_mcp_servers(payload: MCPServersUpdateRequest) -> MCPServersResponse:
    """整表替换并保存系统级 MCP 服务配置。"""

    return _update_mcp_servers(None, payload)


@app.put(f"{_WORKSPACE}/mcp-servers", response_model=MCPServersResponse)
async def update_workspace_mcp_servers(
    workspace_id: int, payload: MCPServersUpdateRequest
) -> MCPServersResponse:
    """整表替换并保存工作区自己的 MCP 服务配置。"""

    return _update_mcp_servers(workspace_id, payload)


@app.post("/configuration/mcp-servers/test", response_model=MCPServerTestResponse)
async def test_system_mcp_server(payload: MCPServerTestRequest) -> MCPServerTestResponse:
    """短连接测试一份系统级 MCP 服务配置（可以尚未保存）。"""

    return await _test_mcp_server(None, payload)


@app.post(f"{_WORKSPACE}/mcp-servers/test", response_model=MCPServerTestResponse)
async def test_workspace_mcp_server(
    workspace_id: int, payload: MCPServerTestRequest
) -> MCPServerTestResponse:
    """短连接测试一份工作区 MCP 服务配置（在工作区根目录运行 stdio 服务）。"""

    return await _test_mcp_server(workspace_id, payload)


def _mcp_service(workspace_id: int | None) -> MCPConfigurationService:
    return MCPConfigurationService(workspace_root=_scope_root(workspace_id))


def _list_mcp_servers(workspace_id: int | None) -> MCPServersResponse:
    try:
        return _mcp_servers_response(_mcp_service(workspace_id).visible())
    except Exception as exc:
        raise_configuration_error(exc)


def _update_mcp_servers(
    workspace_id: int | None, payload: MCPServersUpdateRequest
) -> MCPServersResponse:
    try:
        servers = [
            MCPServerConfig.model_validate(server.model_dump()) for server in payload.servers
        ]
        return _mcp_servers_response(_mcp_service(workspace_id).update(servers))
    except Exception as exc:
        raise_configuration_error(exc)


async def _test_mcp_server(
    workspace_id: int | None, payload: MCPServerTestRequest
) -> MCPServerTestResponse:
    try:
        service = _mcp_service(workspace_id)
        server = MCPServerConfig.model_validate(payload.model_dump())
    except Exception as exc:
        raise_configuration_error(exc)
    result = await service.test(server)
    return MCPServerTestResponse(
        success=result.success,
        tools=result.tools,
        error=result.error,
        agent_available=result.agent_available,
    )


def _mcp_servers_response(servers: list[ScopedMCPServer]) -> MCPServersResponse:
    tested = MCPTestedServers.read()
    return MCPServersResponse(
        servers=[
            MCPServerConfigResponse.model_validate(
                {
                    **server.config.model_dump(),
                    "tested": tested.passed(server),
                    "scope": server.scope.kind,
                    "overrides": server.overrides,
                }
            )
            for server in servers
        ]
    )


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


@app.get("/configuration/skills", response_model=list[SkillInfoResponse])
async def list_system_skills() -> list[SkillInfoResponse]:
    """列出系统 Skill 目录中的有效技能及启用状态。"""

    return _list_skills(None)


@app.get(f"{_WORKSPACE}/skills", response_model=list[SkillInfoResponse])
async def list_workspace_skills(workspace_id: int) -> list[SkillInfoResponse]:
    """列出工作区看得到的 Skill（系统级与工作区合并，同名时工作区覆盖）。"""

    return _list_skills(workspace_id)


@app.get("/configuration/skills/{name}", response_model=SkillDocumentResponse)
async def get_system_skill(name: str) -> SkillDocumentResponse:
    """读取系统级 Skill 的只读详情，不受启用状态限制。"""

    return _read_skill(None, name)


@app.get(f"{_WORKSPACE}/skills/{{name}}", response_model=SkillDocumentResponse)
async def get_workspace_skill(workspace_id: int, name: str) -> SkillDocumentResponse:
    """读取工作区看得到的一个 Skill 的只读详情。"""

    return _read_skill(workspace_id, name)


@app.put("/configuration/skills/{name}/enabled", response_model=SkillInfoResponse)
async def update_system_skill_enabled(
    name: str, payload: SkillEnabledUpdateRequest
) -> SkillInfoResponse:
    """启用或停用一个系统级 Skill。"""

    return _set_skill_enabled(None, name, payload)


@app.put(f"{_WORKSPACE}/skills/{{name}}/enabled", response_model=SkillInfoResponse)
async def update_workspace_skill_enabled(
    workspace_id: int, name: str, payload: SkillEnabledUpdateRequest
) -> SkillInfoResponse:
    """启用或停用工作区自己的一个 Skill；继承来的系统级 Skill 只读（400）。"""

    return _set_skill_enabled(workspace_id, name, payload)


@app.post("/configuration/skills/import", response_model=SkillInfoResponse, status_code=201)
def import_system_skill(payload: SkillsImportRequest) -> SkillInfoResponse:
    """从桌面原生选择器选中的本机路径导入系统级 Skill。"""

    return _import_skill(None, payload)


@app.post(f"{_WORKSPACE}/skills/import", response_model=SkillInfoResponse, status_code=201)
def import_workspace_skill(workspace_id: int, payload: SkillsImportRequest) -> SkillInfoResponse:
    """导入工作区 Skill；与系统级同名时视为有意覆盖。"""

    return _import_skill(workspace_id, payload)


@app.delete("/configuration/skills/{name}", response_model=SkillDeleteResponse)
async def delete_system_skill(name: str) -> SkillDeleteResponse:
    """删除系统目录内的 Skill。"""

    return _delete_skill(None, name)


@app.delete(f"{_WORKSPACE}/skills/{{name}}", response_model=SkillDeleteResponse)
async def delete_workspace_skill(workspace_id: int, name: str) -> SkillDeleteResponse:
    """删除工作区自己的 Skill；继承来的系统级 Skill 只读（400）。"""

    return _delete_skill(workspace_id, name)


def _skill_service(workspace_id: int | None) -> SkillConfigurationService:
    return SkillConfigurationService(workspace_root=_scope_root(workspace_id))


def _list_skills(workspace_id: int | None) -> list[SkillInfoResponse]:
    try:
        return [_skill_response(skill) for skill in _skill_service(workspace_id).list()]
    except Exception as exc:
        raise_configuration_error(exc)


def _read_skill(workspace_id: int | None, name: str) -> SkillDocumentResponse:
    try:
        document = _skill_service(workspace_id).read(name)
        return SkillDocumentResponse(
            **_skill_response(document.info).model_dump(), content=document.body
        )
    except Exception as exc:
        raise_configuration_error(exc)


def _set_skill_enabled(
    workspace_id: int | None, name: str, payload: SkillEnabledUpdateRequest
) -> SkillInfoResponse:
    try:
        return _skill_response(_skill_service(workspace_id).set_enabled(name, payload.enabled))
    except Exception as exc:
        raise_configuration_error(exc)


def _import_skill(workspace_id: int | None, payload: SkillsImportRequest) -> SkillInfoResponse:
    try:
        service = _skill_service(workspace_id)
        if payload.source_type == "directory":
            skill = service.import_directory(payload.source_path)
        else:
            skill = service.import_archive(payload.source_path)
        return _skill_response(skill)
    except Exception as exc:
        raise_configuration_error(exc)


def _delete_skill(workspace_id: int | None, name: str) -> SkillDeleteResponse:
    try:
        _skill_service(workspace_id).delete(name)
        return SkillDeleteResponse(name=name, deleted=True)
    except Exception as exc:
        raise_configuration_error(exc)


def _skill_response(skill: SkillInfo) -> SkillInfoResponse:
    """把目录型领域对象投影为不暴露本机路径的 HTTP 响应。"""

    return SkillInfoResponse(
        name=skill.name,
        description=skill.description,
        enabled=skill.enabled,
        scope=skill.scope.kind,
        overrides=skill.overrides,
    )


def _scope_root(workspace_id: int | None) -> Path | None:
    """``None`` 为系统级；否则解析已登记 workspace 的根目录（不存在 404、根目录失效 400）。"""

    return None if workspace_id is None else resolve_workspace_root(workspace_id)
