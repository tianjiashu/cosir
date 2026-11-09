import asyncio
import json
import socket
import sys
from pathlib import Path

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

from app.core.tool.schemas import ToolExecutionContext
from app.service.configuration.extension_scope import ExtensionScope
from app.service.configuration.mcp_configuration import (
    MCPConfigurationError,
    MCPServerConfig,
    ScopedMCPServer,
    available_servers,
    read_mcp_configuration,
    visible_servers,
)
from app.service.configuration.mcp_tested_servers import MCPTestedServers
from app.service.configuration.skill_catalog import SkillCatalog, SkillLoadError
from app.service.configuration.skill_configuration import read_skill_configuration
from app.tools.mcp.client import _stdio_parameters, call_result_text, list_server_tools
from app.tools.mcp.connection_pool import MCPConnectionPool
from app.tools.tool_handler.mcp_tool import MCPTool
from app.tools.tool_handler.skill_tool import SkillTool
from app.utils.path import system_cosir


def _write_skill(directory, name: str, description: str, body: str = "说明正文"):
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def _system_cosir(tmp_path) -> Path:
    """conftest 把系统级 Skill / MCP 路径隔离到这里。"""

    return tmp_path / "system-cosir"


def _workspace(tmp_path, name: str = "workspace") -> Path:
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    return root


def _write_mcp_servers(cosir_dir: Path, *servers: MCPServerConfig) -> None:
    path = cosir_dir / "config" / "mcp_servers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"servers": [server.model_dump(mode="json") for server in servers]}),
        encoding="utf-8",
    )


def _context(workspace_root: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        task_id=1, workspace_id=1, workspace_root=workspace_root, step_id="step", run_id=1
    )


def test_agent_facing_tool_and_argument_descriptions_are_english():
    for tool in (MCPTool(), SkillTool()):
        descriptions = [tool.description]
        descriptions.extend(
            field["description"]
            for field in tool.args_model.model_json_schema()["properties"].values()
        )

        assert all(
            not any("\u4e00" <= character <= "\u9fff" for character in text)
            for text in descriptions
        )


def test_skill_catalog_merges_workspace_over_system(tmp_path):
    system_root = _system_cosir(tmp_path) / "skills"
    workspace = _workspace(tmp_path)
    _write_skill(system_root / "example", "example", "系统 Skill", "系统正文")
    _write_skill(system_root / "shared", "shared", "系统版本")
    _write_skill(workspace / ".cosir" / "skills" / "shared", "shared", "工作区版本", "工作区正文")
    _write_skill(workspace / ".cosir" / "skills" / "workspace-only", "workspace-only", "工作区 Skill")
    # 只认 ``.cosir/skills``：其他工具的目录约定不参与。
    _write_skill(workspace / ".agents" / "skills" / "foreign", "foreign", "外部约定")

    visible = SkillCatalog.visible_from(workspace).list_skills()
    system_only = SkillCatalog.visible_from(None).list_skills()

    assert [(item.name, item.description, item.scope.kind, item.overrides) for item in visible] == [
        ("example", "系统 Skill", "system", False),
        ("shared", "工作区版本", "workspace", True),
        ("workspace-only", "工作区 Skill", "workspace", False),
    ]
    assert [(item.name, item.description) for item in system_only] == [
        ("example", "系统 Skill"),
        ("shared", "系统版本"),
    ]
    assert "工作区正文" in SkillCatalog.visible_from(workspace).load_enabled("shared").body


def test_skill_document_for_model_names_its_directory_and_resources_load(tmp_path):
    workspace = _workspace(tmp_path)
    directory = workspace / ".cosir" / "skills" / "example"
    _write_skill(directory, "example", "测试 Skill", "运行 scripts/run.py")
    (directory / "reference.md").write_text("资源正文", encoding="utf-8")
    catalog = SkillCatalog.visible_from(workspace)

    text = catalog.load_enabled("example").for_model()

    assert str(directory.resolve()) in text
    assert text.endswith("运行 scripts/run.py\n")
    info, resource = catalog.load_resource("example", "reference.md")
    assert (info.scope.kind, resource) == ("workspace", "资源正文")


def test_skill_catalog_rejects_resource_escape(tmp_path):
    system_root = _system_cosir(tmp_path) / "skills"
    _write_skill(system_root / "example", "example", "测试 Skill")
    (system_root / "outside.md").write_text("外部文件", encoding="utf-8")

    with pytest.raises(SkillLoadError, match="Skill 目录内"):
        SkillCatalog.visible_from(None).load_resource("example", "../outside.md")


def test_each_scope_enable_list_only_governs_its_own_skills(tmp_path):
    system_cosir_dir = _system_cosir(tmp_path)
    workspace = _workspace(tmp_path)
    _write_skill(system_cosir_dir / "skills" / "system-skill", "system-skill", "系统")
    _write_skill(workspace / ".cosir" / "skills" / "local-skill", "local-skill", "工作区")
    (workspace / ".cosir" / "config").mkdir(parents=True)
    # workspace 名单里列出系统级 Skill 也不能启停它；没列出的工作区 Skill 被停用。
    (workspace / ".cosir" / "config" / "skills.json").write_text(
        '{"enabled_skills":["system-skill"]}', encoding="utf-8"
    )

    visible = {item.name: item.enabled for item in SkillCatalog.visible_from(workspace).list_skills()}

    assert visible == {"local-skill": False, "system-skill": True}


def test_missing_skill_configuration_enables_all_installed_skills(tmp_path):
    workspace = _workspace(tmp_path)

    assert read_skill_configuration(ExtensionScope.workspace(workspace)).is_enabled("newly-installed")
    assert read_skill_configuration(ExtensionScope.system()).is_enabled("newly-installed")


def test_mcp_visible_servers_merge_workspace_over_system(tmp_path):
    workspace = _workspace(tmp_path)
    _write_mcp_servers(
        _system_cosir(tmp_path),
        MCPServerConfig(name="shared", command="system-cmd"),
        MCPServerConfig(name="system-only", command="python"),
    )
    _write_mcp_servers(
        workspace / ".cosir",
        MCPServerConfig(name="shared", command="workspace-cmd"),
        MCPServerConfig(name="workspace-only", command="node"),
    )

    visible = visible_servers(workspace)

    assert [
        (server.name, server.config.command, server.scope.kind, server.overrides) for server in visible
    ] == [
        ("system-only", "python", "system", False),
        ("shared", "workspace-cmd", "workspace", True),
        ("workspace-only", "node", "workspace", False),
    ]
    assert [server.config.command for server in visible_servers(None)] == ["system-cmd", "python"]


def test_mcp_available_servers_require_enabled_and_local_test_record(tmp_path):
    workspace = _workspace(tmp_path)
    tested = MCPServerConfig(name="tested", command="python")
    disabled = MCPServerConfig(name="disabled", command="python", enabled=False)
    untested = MCPServerConfig(name="untested", command="python")
    _write_mcp_servers(workspace / ".cosir", tested, disabled, untested)
    scope = ExtensionScope.workspace(workspace)
    MCPTestedServers.record_pass(ScopedMCPServer(scope, tested))
    MCPTestedServers.record_pass(ScopedMCPServer(scope, disabled))

    assert [server.name for server in available_servers(workspace)] == ["tested"]
    # 同一服务在另一个工作区没有测试记录。
    other = _workspace(tmp_path, "other")
    _write_mcp_servers(other / ".cosir", tested)
    assert available_servers(other) == []


def test_mcp_config_file_cannot_carry_test_results(tmp_path):
    """测试结果只认本机记录：配置文件里夹带测试字段按格式无效拒绝，不能让服务变为可用。"""

    workspace = _workspace(tmp_path)
    path = workspace / ".cosir" / "config" / "mcp_servers.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "servers": [{"name": "planted", "command": "python"}],
                "tested_servers": {"planted": "forged"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(MCPConfigurationError, match="格式无效"):
        available_servers(workspace)


def test_mcp_test_record_keeps_saved_config_when_a_draft_is_tested(tmp_path):
    """测一份草稿（成功或失败）都不能顶掉已保存配置的测试记录。"""

    scope = ExtensionScope.workspace(_workspace(tmp_path))
    saved = ScopedMCPServer(scope, MCPServerConfig(name="one", command="python"))
    draft = ScopedMCPServer(scope, MCPServerConfig(name="one", command="node"))
    MCPTestedServers.record_pass(saved)

    MCPTestedServers.record_pass(draft)
    assert MCPTestedServers.read().passed(saved)
    assert MCPTestedServers.revoke(draft) is True
    assert MCPTestedServers.read().passed(saved)
    assert MCPTestedServers.revoke(draft) is False
    assert MCPTestedServers.revoke(saved) is True
    assert not MCPTestedServers.read().passed(saved)


def test_mcp_test_record_is_pruned_on_save_and_dropped_with_the_workspace(tmp_path):
    workspace = _workspace(tmp_path)
    scope = ExtensionScope.workspace(workspace)
    kept = ScopedMCPServer(scope, MCPServerConfig(name="kept", command="python"))
    removed = ScopedMCPServer(scope, MCPServerConfig(name="removed", command="python"))
    stale_draft = ScopedMCPServer(scope, MCPServerConfig(name="kept", command="node"))
    for server in (kept, removed, stale_draft):
        MCPTestedServers.record_pass(server)

    MCPTestedServers.keep_only(scope, [kept])
    record = MCPTestedServers.read()

    assert record.passed(kept)
    # 删掉的服务再加回同一配置、残留的草稿指纹，都要重新测试。
    assert not record.passed(removed)
    assert not record.passed(stale_draft)
    MCPTestedServers.drop_scope(scope)
    assert scope.key not in MCPTestedServers.read().scopes


def test_mcp_tool_does_not_discover_or_call_an_untested_server(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    _write_mcp_servers(workspace / ".cosir", MCPServerConfig(name="one", command="python"))
    monkeypatch.setattr(
        "app.service.depends.get_mcp_connection_pool",
        lambda: pytest.fail("untested MCP server must not be discovered or called"),
    )

    tool = MCPTool()
    listing = tool.execute(action="list", execution_context=_context(workspace))
    call = tool.execute(
        action="call", server_name="one", tool_name="search", execution_context=_context(workspace)
    )

    assert json.loads(listing.content) == []
    assert call.status == "error"


def test_mcp_configuration_rejects_duplicate_names(tmp_path):
    workspace = _workspace(tmp_path)
    _write_mcp_servers(
        workspace / ".cosir",
        MCPServerConfig(name="same", command="one"),
        MCPServerConfig(name="same", command="two"),
    )

    with pytest.raises(MCPConfigurationError, match="必须唯一"):
        read_mcp_configuration(ExtensionScope.workspace(workspace))


@pytest.mark.parametrize(
    ("transport", "path"),
    [("sse", "/sse"), ("streamable_http", "/mcp")],
)
def test_mcp_http_transports_list_and_call_tools(transport, path):
    async def exercise_transport():
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        mcp_server = FastMCP("test-server", host="127.0.0.1", port=port)

        @mcp_server.tool()
        def echo(value: str) -> dict:
            return {"echo": value}

        app = mcp_server.sse_app() if transport == "sse" else mcp_server.streamable_http_app()

        async def require_test_header(request, call_next):
            if request.headers.get("x-mcp-test") != "present":
                return PlainTextResponse("missing test header", status_code=401)
            return await call_next(request)

        app.add_middleware(BaseHTTPMiddleware, dispatch=require_test_header)
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical"))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(500):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started

            config = ScopedMCPServer(
                ExtensionScope.system(),
                MCPServerConfig(
                    name="test-server",
                    transport=transport,
                    url=f"http://127.0.0.1:{port}{path}",
                    headers={"x-mcp-test": "present"},
                ),
            )
            tools = await list_server_tools(config)
            pool = MCPConnectionPool()
            try:
                result = await asyncio.wrap_future(
                    pool.submit(pool.call_tool(config, "echo", {"value": "ok"}))
                )
            finally:
                await asyncio.to_thread(pool.close)
        finally:
            server.should_exit = True
            await server_task

        assert [tool["name"] for tool in tools] == ["echo"]
        assert json.loads(call_result_text(result)) == {"echo": "ok"}

    asyncio.run(exercise_transport())


def test_mcp_configuration_requires_transport_specific_endpoint():
    with pytest.raises(ValueError, match="stdio MCP 服务必须配置 command"):
        MCPServerConfig(name="stdio")

    with pytest.raises(ValueError, match="HTTP MCP 服务必须配置 url"):
        MCPServerConfig(name="http", transport="streamable_http")

    config = MCPServerConfig(name="http", transport="sse", url="HTTPS://example.com/sse")
    assert config.url == "HTTPS://example.com/sse"


def test_stdio_parameters_pass_explicit_environment_and_scope_cwd(tmp_path, monkeypatch):
    """MCP 子进程接收服务专属变量（SDK 另行补入默认白名单）；workspace 服务在工作区根目录运行。"""

    monkeypatch.setenv("MODEL_PROVIDER_SECRET", "must-not-be-inherited")
    config = MCPServerConfig(name="stdio", command="mcp-server", env={"SERVER_KEY": "value"})
    workspace = _workspace(tmp_path)

    system_parameters = _stdio_parameters(ScopedMCPServer(ExtensionScope.system(), config))
    workspace_parameters = _stdio_parameters(
        ScopedMCPServer(ExtensionScope.workspace(workspace), config)
    )

    assert system_parameters.env == {"SERVER_KEY": "value"}
    assert system_parameters.cwd is None
    assert Path(workspace_parameters.cwd) == workspace


_CWD_SERVER_CODE = (
    "import os, sys\n"
    "from mcp.server.fastmcp import FastMCP\n"
    "server = FastMCP('test-server')\n"
    "@server.tool()\n"
    "def whoami() -> dict:\n"
    "    return {'marker': sys.argv[1], 'cwd': os.getcwd()}\n"
    "server.run(transport='stdio')\n"
)


def _cwd_server(marker: str) -> MCPServerConfig:
    return MCPServerConfig(
        name="test-server", command=sys.executable, args=["-c", _CWD_SERVER_CODE, marker]
    )


def test_mcp_tool_keeps_same_named_workspace_servers_apart(tmp_path, monkeypatch):
    """两个工作区的同名服务各自连接、各在自己的工作区运行，互不顶替。"""

    first, second = _workspace(tmp_path, "first"), _workspace(tmp_path, "second")
    for workspace, marker in ((first, "first"), (second, "second")):
        config = _cwd_server(marker)
        _write_mcp_servers(workspace / ".cosir", config)
        MCPTestedServers.record_pass(ScopedMCPServer(ExtensionScope.workspace(workspace), config))
    monkeypatch.setattr(system_cosir, "LOG_DIR", tmp_path / "logs")
    pool = MCPConnectionPool()
    monkeypatch.setattr("app.service.depends.get_mcp_connection_pool", lambda: pool)
    tool = MCPTool()

    def call(workspace: Path) -> dict:
        result = tool.execute(
            action="call",
            server_name="test-server",
            tool_name="whoami",
            arguments={},
            execution_context=_context(workspace),
        )
        assert result.status == "success", result.content
        return json.loads(result.content)

    try:
        listing = tool.execute(action="list", execution_context=_context(first))
        results = [call(first), call(second), call(first)]
        connection_count = len(pool._connections)
    finally:
        pool.close()

    assert json.loads(listing.content)[0]["tools"][0]["name"] == "whoami"
    assert json.loads(listing.content)[0]["scope"] == "workspace"
    assert [item["marker"] for item in results] == ["first", "second", "first"]
    assert [Path(item["cwd"]).resolve() for item in results] == [
        first.resolve(),
        second.resolve(),
        first.resolve(),
    ]
    assert connection_count == 2
    # stderr 日志按工作区分文件。
    assert len(list((tmp_path / "logs" / "mcp").glob("test-server.*.stderr.log"))) == 2


def test_skill_tool_lists_and_loads_workspace_visible_skills(tmp_path):
    system_root = _system_cosir(tmp_path) / "skills"
    workspace = _workspace(tmp_path)
    _write_skill(system_root / "example", "example", "系统 Skill", "按需加载的正文")
    _write_skill(system_root / "disabled", "disabled", "禁用 Skill")
    _write_skill(workspace / ".cosir" / "skills" / "local", "local", "工作区 Skill", "工作区正文")
    config_path = _system_cosir(tmp_path) / "config" / "skills.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('{"enabled_skills":["example"]}', encoding="utf-8")
    tool = SkillTool()
    context = _context(workspace)

    listing = tool.execute(action="list", execution_context=context)
    loaded = tool.execute(action="load", skill_name="local", execution_context=context)
    disabled = tool.execute(action="load", skill_name="disabled", execution_context=context)

    assert listing.status == "success"
    assert json.loads(listing.content) == [
        {"name": "example", "description": "系统 Skill"},
        {"name": "local", "description": "工作区 Skill"},
    ]
    assert [item["scope"] for item in listing.display_data["skills"]] == ["system", "workspace"]
    assert loaded.status == "success"
    assert loaded.display_data["scope"] == "workspace"
    assert str((workspace / ".cosir" / "skills" / "local").resolve()) in loaded.content
    assert loaded.content.endswith("工作区正文\n")
    assert disabled.status == "error"
