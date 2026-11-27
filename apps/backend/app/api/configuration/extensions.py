"""MCP 服务与 Skill 配置路由：系统级（``/configuration/...``）与 workspace 级
（``/workspaces/{workspace_id}/configuration/...``）。

两个作用域的路由形状一致，按能力放在同一个模块里；workspace 根目录只经 ``workspace_id`` 由
``resolve_workspace_root`` 解析，不接受客户端路径。列表返回本作用域看得到的合并结果（每项带
``scope`` / ``overrides``），写操作只作用于本作用域。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.api.configuration.errors import raise_configuration_error
from app.api.schemas.request.MCPServerEnabledUpdateRequest import MCPServerEnabledUpdateRequest
from app.api.schemas.request.SkillsEnabledUpdateRequest import SkillsEnabledUpdateRequest
from app.api.schemas.request.SkillsImportRequest import SkillsImportRequest
from app.api.schemas.response.MCPServersResponse import MCPServersResponse
from app.api.schemas.response.MCPServerTestResponse import (
    MCPAuthorizationPendingResponse,
    MCPServerAuthorizeResponse,
    MCPServerTestResponse,
)
from app.api.schemas.response.SkillCatalogViewResponse import (
    SkillCatalogViewResponse,
    SkillEntryResponse,
    SkillGroupResponse,
    SkippedSkillResponse,
)
from app.api.schemas.response.SkillDetailResponse import (
    SkillDetailResponse,
    SkillFileContentResponse,
    SkillFileResponse,
)
from app.api.schemas.response.SkillInfoResponse import SkillInfoResponse
from app.app import app
from app.service.configuration.mcp.configuration import MCPServerConfig
from app.service.configuration.mcp.configuration_service import (
    MCPConfigurationService,
    MCPServerTestResult,
    PendingAuthorization,
)
from app.service.configuration.mcp.configuration_view import MCPServerEntry
from app.service.configuration.skills.catalog import SkillInfo
from app.service.configuration.skills.configuration_service import SkillConfigurationService
from app.service.configuration.skills.configuration_view import (
    SkillCatalogView,
    SkillEntry,
    SkillGroup,
)
from app.service.configuration.skills.skill_import import SkillImporter
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


# 写操作按服务寻址、只表达这一项的意图（新增、替换、启停、删除），后端在文件锁内基于最新文件合并：
# 配置文件可以在外部改，配置页手上的列表可能已过期，不接收整表覆盖。


@app.post("/configuration/mcp-servers", response_model=MCPServersResponse)
async def create_system_mcp_server(payload: MCPServerConfig) -> MCPServersResponse:
    """新增一个系统级 MCP 服务。"""

    return _write_mcp_servers(None, lambda service: service.create(payload))


@app.post(f"{_WORKSPACE}/mcp-servers", response_model=MCPServersResponse)
async def create_workspace_mcp_server(
    workspace_id: int, payload: MCPServerConfig
) -> MCPServersResponse:
    """新增一个工作区自己的 MCP 服务。"""

    return _write_mcp_servers(workspace_id, lambda service: service.create(payload))


@app.put("/configuration/mcp-servers/{name}", response_model=MCPServersResponse)
async def replace_system_mcp_server(name: str, payload: MCPServerConfig) -> MCPServersResponse:
    """替换一个系统级 MCP 服务的定义（``payload.name`` 不同即改名）。"""

    return _write_mcp_servers(None, lambda service: service.replace(name, payload))


@app.put(f"{_WORKSPACE}/mcp-servers/{{name}}", response_model=MCPServersResponse)
async def replace_workspace_mcp_server(
    workspace_id: int, name: str, payload: MCPServerConfig
) -> MCPServersResponse:
    """替换一个工作区 MCP 服务的定义（``payload.name`` 不同即改名）。"""

    return _write_mcp_servers(workspace_id, lambda service: service.replace(name, payload))


@app.patch("/configuration/mcp-servers/{name}", response_model=MCPServersResponse)
async def set_system_mcp_server_enabled(
    name: str, payload: MCPServerEnabledUpdateRequest
) -> MCPServersResponse:
    """启用或停用一个系统级 MCP 服务。"""

    return _write_mcp_servers(None, lambda service: service.set_enabled(name, payload.enabled))


@app.patch(f"{_WORKSPACE}/mcp-servers/{{name}}", response_model=MCPServersResponse)
async def set_workspace_mcp_server_enabled(
    workspace_id: int, name: str, payload: MCPServerEnabledUpdateRequest
) -> MCPServersResponse:
    """启用或停用一个工作区 MCP 服务。"""

    return _write_mcp_servers(workspace_id, lambda service: service.set_enabled(name, payload.enabled))


@app.delete("/configuration/mcp-servers/{name}", response_model=MCPServersResponse)
async def delete_system_mcp_server(name: str) -> MCPServersResponse:
    """删除一个系统级 MCP 服务；已经不在时照常返回列表。"""

    return _write_mcp_servers(None, lambda service: service.delete(name))


@app.delete(f"{_WORKSPACE}/mcp-servers/{{name}}", response_model=MCPServersResponse)
async def delete_workspace_mcp_server(workspace_id: int, name: str) -> MCPServersResponse:
    """删除一个工作区 MCP 服务；已经不在时照常返回列表。"""

    return _write_mcp_servers(workspace_id, lambda service: service.delete(name))


@app.post("/configuration/mcp-servers/test", response_model=MCPServerTestResponse)
async def test_system_mcp_server(payload: MCPServerConfig) -> MCPServerTestResponse:
    """短连接测试一份系统级 MCP 服务配置（可以尚未保存）；从不打开浏览器。"""

    return await _test_mcp_server(None, payload)


@app.post(f"{_WORKSPACE}/mcp-servers/test", response_model=MCPServerTestResponse)
async def test_workspace_mcp_server(
    workspace_id: int, payload: MCPServerConfig
) -> MCPServerTestResponse:
    """短连接测试一份工作区 MCP 服务配置（在工作区根目录运行 stdio 服务）；从不打开浏览器。"""

    return await _test_mcp_server(workspace_id, payload)


@app.post("/configuration/mcp-servers/authorize", response_model=MCPServerAuthorizeResponse)
async def authorize_system_mcp_server(payload: MCPServerConfig) -> MCPServerAuthorizeResponse:
    """开始登录授权一份系统级 MCP 服务配置（可以尚未保存）。"""

    return await _authorize_mcp_server(None, payload)


@app.post(f"{_WORKSPACE}/mcp-servers/authorize", response_model=MCPServerAuthorizeResponse)
async def authorize_workspace_mcp_server(
    workspace_id: int, payload: MCPServerConfig
) -> MCPServerAuthorizeResponse:
    """开始登录授权一份工作区 MCP 服务配置。"""

    return await _authorize_mcp_server(workspace_id, payload)


@app.post("/configuration/mcp-authorizations/{flow_id}/wait", response_model=MCPServerTestResponse)
async def wait_mcp_authorization(flow_id: str) -> MCPServerTestResponse:
    """等一次登录授权结束（用户在浏览器里完成、取消或超时）；授权按 id 全局唯一，不分作用域。"""

    try:
        return _test_response(await MCPConfigurationService.wait_authorization(flow_id))
    except Exception as exc:
        raise_configuration_error(exc)


@app.delete("/configuration/mcp-authorizations/{flow_id}")
async def cancel_mcp_authorization(flow_id: str) -> dict:
    """放弃一次登录授权：后端结束等待并关掉回调监听。"""

    try:
        MCPConfigurationService.cancel_authorization(flow_id)
    except Exception as exc:
        raise_configuration_error(exc)
    return {}


def _mcp_service(workspace_id: int | None) -> MCPConfigurationService:
    return MCPConfigurationService(workspace_root=_scope_root(workspace_id))


def _list_mcp_servers(workspace_id: int | None) -> MCPServersResponse:
    try:
        return MCPServersResponse.from_entries(_mcp_service(workspace_id).visible())
    except Exception as exc:
        raise_configuration_error(exc)


def _write_mcp_servers(
    workspace_id: int | None,
    write: Callable[[MCPConfigurationService], list[MCPServerEntry]],
) -> MCPServersResponse:
    try:
        return MCPServersResponse.from_entries(write(_mcp_service(workspace_id)))
    except Exception as exc:
        raise_configuration_error(exc)


async def _test_mcp_server(workspace_id: int | None, payload: MCPServerConfig) -> MCPServerTestResponse:
    try:
        service = _mcp_service(workspace_id)
    except Exception as exc:
        raise_configuration_error(exc)
    return _test_response(await service.test(payload))


async def _authorize_mcp_server(
    workspace_id: int | None, payload: MCPServerConfig
) -> MCPServerAuthorizeResponse:
    try:
        service = _mcp_service(workspace_id)
    except Exception as exc:
        raise_configuration_error(exc)
    result = await service.authorize(payload)
    if isinstance(result, PendingAuthorization):
        return MCPAuthorizationPendingResponse(
            flow_id=result.flow_id, authorization_url=result.authorization_url
        )
    return _test_response(result)


def _test_response(result: MCPServerTestResult) -> MCPServerTestResponse:
    return MCPServerTestResponse(
        success=result.success,
        tools=result.tools,
        error=result.error,
        failure_kind=result.failure_kind,
        needs_sign_in=result.needs_sign_in,
        agent_available=result.agent_available,
    )


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


@app.get("/configuration/skills", response_model=list[SkillInfoResponse])
async def list_system_skills() -> list[SkillInfoResponse]:
    """列出系统级生效的 Skill（输入框选择器用）。"""

    return _list_skills(None)


@app.get(f"{_WORKSPACE}/skills", response_model=list[SkillInfoResponse])
async def list_workspace_skills(workspace_id: int) -> list[SkillInfoResponse]:
    """列出工作区看得到的生效 Skill（系统级与工作区合并，同名时工作区覆盖）。"""

    return _list_skills(workspace_id)


@app.get("/configuration/skill-catalog", response_model=SkillCatalogViewResponse)
async def get_system_skill_catalog() -> SkillCatalogViewResponse:
    """系统级配置页的目录：Skill、处境与 Agent 目录体量。"""

    return _skill_catalog(None)


@app.get(f"{_WORKSPACE}/skill-catalog", response_model=SkillCatalogViewResponse)
async def get_workspace_skill_catalog(workspace_id: int) -> SkillCatalogViewResponse:
    """工作区配置页的分组目录：本工作区在前，继承来的系统级在后（只读）。"""

    return _skill_catalog(workspace_id)


@app.put("/configuration/skills/enabled")
async def update_system_skills_enabled(payload: SkillsEnabledUpdateRequest) -> dict[str, object]:
    """一次启用或停用若干个系统级 Skill。"""

    return _set_skills_enabled(None, payload)


@app.put(f"{_WORKSPACE}/skills/enabled")
async def update_workspace_skills_enabled(
    workspace_id: int, payload: SkillsEnabledUpdateRequest
) -> dict[str, object]:
    """一次启用或停用工作区自己的若干个 Skill；含继承来的系统级 Skill 时整体拒绝（400）。"""

    return _set_skills_enabled(workspace_id, payload)


@app.get("/configuration/skills/{name}", response_model=SkillDetailResponse)
async def get_system_skill(name: str) -> SkillDetailResponse:
    """读取系统级 Skill 的只读详情，不受启用状态限制。"""

    return _read_skill(None, name)


@app.get(f"{_WORKSPACE}/skills/{{name}}", response_model=SkillDetailResponse)
async def get_workspace_skill(workspace_id: int, name: str) -> SkillDetailResponse:
    """读取工作区看得到的一个 Skill 的只读详情。"""

    return _read_skill(workspace_id, name)


@app.get("/configuration/skills/{name}/file", response_model=SkillFileContentResponse)
async def get_system_skill_file(name: str, path: str) -> SkillFileContentResponse:
    """读取系统级 Skill 目录内一个文本文件的只读预览。"""

    return _preview_skill_file(None, name, path)


@app.get(f"{_WORKSPACE}/skills/{{name}}/file", response_model=SkillFileContentResponse)
async def get_workspace_skill_file(
    workspace_id: int, name: str, path: str
) -> SkillFileContentResponse:
    """读取工作区看得到的一个 Skill 目录内文本文件的只读预览。"""

    return _preview_skill_file(workspace_id, name, path)


@app.post("/configuration/skills/import", response_model=SkillInfoResponse, status_code=201)
def import_system_skill(payload: SkillsImportRequest) -> SkillInfoResponse:
    """从桌面原生选择器选中的本机路径导入系统级 Skill。"""

    return _import_skill(None, payload)


@app.post(f"{_WORKSPACE}/skills/import", response_model=SkillInfoResponse, status_code=201)
def import_workspace_skill(workspace_id: int, payload: SkillsImportRequest) -> SkillInfoResponse:
    """导入工作区 Skill；与系统级同名时视为有意覆盖。"""

    return _import_skill(workspace_id, payload)


def _skill_service(workspace_id: int | None) -> SkillConfigurationService:
    return SkillConfigurationService(workspace_root=_scope_root(workspace_id))


def _list_skills(workspace_id: int | None) -> list[SkillInfoResponse]:
    try:
        return [_skill_response(skill) for skill in _skill_service(workspace_id).list()]
    except Exception as exc:
        raise_configuration_error(exc)


def _skill_catalog(workspace_id: int | None) -> SkillCatalogViewResponse:
    try:
        return _skill_catalog_response(_skill_service(workspace_id).view())
    except Exception as exc:
        raise_configuration_error(exc)


def _read_skill(workspace_id: int | None, name: str) -> SkillDetailResponse:
    try:
        detail = _skill_service(workspace_id).read(name)
        return SkillDetailResponse(
            **_skill_response(detail.info).model_dump(),
            content=detail.body,
            also_at=list(detail.also_at),
            directory=str(detail.directory),
            metadata=detail.metadata,
            files=[
                SkillFileResponse(path=item.path, size=item.size, binary=item.binary)
                for item in detail.files
            ],
            files_truncated=detail.files_truncated,
        )
    except Exception as exc:
        raise_configuration_error(exc)


def _preview_skill_file(workspace_id: int | None, name: str, path: str) -> SkillFileContentResponse:
    try:
        content = _skill_service(workspace_id).preview_file(name, path)
        return SkillFileContentResponse(path=path, content=content)
    except Exception as exc:
        raise_configuration_error(exc)


def _set_skills_enabled(
    workspace_id: int | None, payload: SkillsEnabledUpdateRequest
) -> dict[str, object]:
    try:
        _skill_service(workspace_id).set_enabled(payload.names, payload.enabled)
        return {}
    except Exception as exc:
        raise_configuration_error(exc)


def _import_skill(workspace_id: int | None, payload: SkillsImportRequest) -> SkillInfoResponse:
    try:
        importer = SkillImporter(workspace_root=_scope_root(workspace_id))
        if payload.source_type == "directory":
            skill = importer.import_directory(payload.source_path, payload.resolution)
        else:
            skill = importer.import_archive(payload.source_path, payload.resolution)
        return _skill_response(skill)
    except Exception as exc:
        raise_configuration_error(exc)


def _skill_response(skill: SkillInfo) -> SkillInfoResponse:
    """把目录型领域对象投影为不暴露本机路径的列表项（本机路径只在详情里给「在文件夹中显示」用）。"""

    return SkillInfoResponse(
        name=skill.name,
        description=skill.description,
        enabled=skill.enabled,
        scope=skill.scope.kind,
        kind=skill.root.kind,
        overrides=skill.overrides,
        warnings=list(skill.warnings),
        manual_only=skill.manual_only,
        has_scripts=skill.has_scripts,
    )


def _skill_catalog_response(view: SkillCatalogView) -> SkillCatalogViewResponse:
    return SkillCatalogViewResponse(
        agent_skills=view.agent_skills,
        agent_tokens=view.agent_tokens,
        groups=[_skill_group_response(group) for group in view.groups],
    )


def _skill_group_response(group: SkillGroup) -> SkillGroupResponse:
    return SkillGroupResponse(
        scope=group.root.scope.kind,
        kind=group.root.kind,
        location=group.root.location,
        skipped=[
            SkippedSkillResponse(directory=item.directory, reason=item.reason)
            for item in group.skipped
        ],
        skills=[_skill_entry_response(entry) for entry in group.entries],
    )


def _skill_entry_response(entry: SkillEntry) -> SkillEntryResponse:
    return SkillEntryResponse(**_skill_response(entry.info).model_dump(), standing=entry.standing)


def _scope_root(workspace_id: int | None) -> Path | None:
    """``None`` 为系统级；否则解析已登记 workspace 的根目录（不存在 404、根目录失效 400）。"""

    return None if workspace_id is None else resolve_workspace_root(workspace_id)
