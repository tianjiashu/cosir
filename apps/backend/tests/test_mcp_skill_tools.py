import asyncio
import json
import socket
import sys

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

from app.tools.tool_handler.mcp_tool import MCPTool
from app.tools.tool_handler.skill_tool import SkillTool
from app.service.configuration.mcp_client import (
    MCPConfigurationError,
    MCPServerConfig,
    _stdio_parameters,
    call_server_tool,
    enabled_servers,
    list_server_tools,
    read_mcp_configuration,
    server_connection_fingerprint,
)
from app.service.configuration.skill_catalog import SkillCatalog, SkillLoadError
from app.service.configuration.skill_configuration import read_skill_configuration


def _write_skill(directory, name: str, description: str, body: str = "说明正文"):
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
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


def test_skill_catalog_reads_only_system_skills_and_loads_local_resource(tmp_path, monkeypatch):
    system_root = tmp_path / "system" / "skills"
    workspace_root = tmp_path / "workspace" / ".agents" / "skills"
    _write_skill(system_root / "example", "example", "系统 Skill", "系统正文")
    _write_skill(workspace_root / "workspace-only", "workspace-only", "工作区 Skill")
    (system_root / "example" / "reference.md").write_text("资源正文", encoding="utf-8")
    monkeypatch.setattr("app.service.configuration.skill_catalog.system_skills_dir", lambda: system_root)
    monkeypatch.setattr(
        "app.service.configuration.skill_configuration.system_skills_config_file",
        lambda: tmp_path / "missing-skills.json",
    )

    catalog = SkillCatalog()

    skills = catalog.list_skills()
    skill, body = catalog.load("example")
    _, resource = catalog.load("example", "reference.md")

    assert [(item.name, item.description, item.enabled) for item in skills] == [
        ("example", "系统 Skill", True)
    ]
    assert (skill.enabled, body, resource) == (True, "系统正文\n", "资源正文")


def test_skill_catalog_rejects_resource_escape(tmp_path, monkeypatch):
    system_root = tmp_path / "system" / "skills"
    _write_skill(system_root / "example", "example", "测试 Skill")
    (system_root / "outside.md").write_text("外部文件", encoding="utf-8")
    monkeypatch.setattr("app.service.configuration.skill_catalog.system_skills_dir", lambda: system_root)
    monkeypatch.setattr(
        "app.service.configuration.skill_configuration.system_skills_config_file",
        lambda: tmp_path / "missing-skills.json",
    )

    with pytest.raises(SkillLoadError, match="Skill 目录内"):
        SkillCatalog().load("example", "../outside.md")


def test_mcp_configuration_reads_enabled_servers(tmp_path, monkeypatch):
    config_path = tmp_path / "mcp_servers.json"
    verified = MCPServerConfig(name="one", command="python")
    config_path.write_text(
        json.dumps(
            {
                "servers": [
                    verified.model_dump(mode="json"),
                    {"name": "two", "command": "node", "enabled": False},
                ],
                "tested_servers": {"one": server_connection_fingerprint(verified)},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.service.configuration.mcp_client.system_mcp_servers_file", lambda: config_path)

    configuration = read_mcp_configuration()

    assert [server.name for server in enabled_servers(configuration)] == ["one"]


def test_mcp_configuration_excludes_enabled_servers_without_successful_connection_test(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text('{"servers":[{"name":"one","command":"python"}]}', encoding="utf-8")
    monkeypatch.setattr("app.service.configuration.mcp_client.system_mcp_servers_file", lambda: config_path)

    assert enabled_servers(read_mcp_configuration()) == []


def test_mcp_tool_does_not_discover_or_call_an_untested_server(tmp_path, monkeypatch):
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text('{"servers":[{"name":"one","command":"python"}]}', encoding="utf-8")
    monkeypatch.setattr("app.service.configuration.mcp_client.system_mcp_servers_file", lambda: config_path)
    monkeypatch.setattr(
        "app.tools.tool_handler.mcp_tool.list_server_tools",
        lambda server: pytest.fail("untested MCP server must not be discovered"),
    )
    monkeypatch.setattr(
        "app.tools.tool_handler.mcp_tool.call_server_tool",
        lambda *args: pytest.fail("untested MCP server must not be called"),
    )

    tool = MCPTool()
    listing = tool.execute(action="list")
    call = tool.execute(action="call", server_name="one", tool_name="search")

    assert json.loads(listing.content) == []
    assert call.status == "error"


def test_mcp_configuration_rejects_duplicate_names(tmp_path, monkeypatch):
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(
        '{"servers":[{"name":"same","command":"one"},' '{"name":"same","command":"two"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr("app.service.configuration.mcp_client.system_mcp_servers_file", lambda: config_path)

    with pytest.raises(MCPConfigurationError, match="必须唯一"):
        read_mcp_configuration()


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

            config = MCPServerConfig(
                name="test-server",
                transport=transport,
                url=f"http://127.0.0.1:{port}{path}",
                headers={"x-mcp-test": "present"},
            )
            tools = await list_server_tools(config)
            result = await call_server_tool(config, "echo", {"value": "ok"})
        finally:
            server.should_exit = True
            await server_task

        assert [tool["name"] for tool in tools] == ["echo"]
        assert json.loads(result) == {"echo": "ok"}

    asyncio.run(exercise_transport())


def test_mcp_configuration_requires_transport_specific_endpoint():
    with pytest.raises(ValueError, match="stdio MCP 服务必须配置 command"):
        MCPServerConfig(name="stdio")

    with pytest.raises(ValueError, match="HTTP MCP 服务必须配置 url"):
        MCPServerConfig(name="http", transport="streamable_http")

    config = MCPServerConfig(name="http", transport="sse", url="HTTPS://example.com/sse")
    assert config.url == "HTTPS://example.com/sse"


def test_stdio_parameters_only_pass_explicit_server_environment(monkeypatch):
    """MCP 子进程接收服务专属变量，SDK 会另行补入默认白名单。"""

    monkeypatch.setenv("MODEL_PROVIDER_SECRET", "must-not-be-inherited")
    server = MCPServerConfig(name="stdio", command="mcp-server", env={"SERVER_KEY": "value"})

    parameters = _stdio_parameters(server)

    assert parameters.env == {"SERVER_KEY": "value"}


def test_mcp_tool_lists_and_calls_local_server(tmp_path, monkeypatch):
    server_code = (
        "from mcp.server.fastmcp import FastMCP\n"
        "server = FastMCP('test-server')\n"
        "@server.tool()\n"
        "def echo(value: str) -> dict:\n"
        "    return {'echo': value}\n"
        "server.run(transport='stdio')\n"
    )
    config_path = tmp_path / "mcp_servers.json"
    server_config = MCPServerConfig(
        name="test-server",
        command=sys.executable,
        args=["-c", server_code],
    )
    config_path.write_text(
        json.dumps(
            {
                "servers": [server_config.model_dump(mode="json")],
                "tested_servers": {
                    server_config.name: server_connection_fingerprint(server_config)
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.service.configuration.mcp_client.system_mcp_servers_file", lambda: config_path)
    tool = MCPTool()

    listing = tool.execute(action="list")
    call = tool.execute(
        action="call",
        server_name="test-server",
        tool_name="echo",
        arguments={"value": "ok"},
    )

    assert listing.status == "success"
    assert json.loads(listing.content)[0]["tools"][0]["name"] == "echo"
    assert call.status == "success"
    assert json.loads(call.content) == {"echo": "ok"}


def test_skill_tool_lists_and_loads_enabled_system_skill(tmp_path, monkeypatch):
    system_root = tmp_path / "system" / "skills"
    skill_directory = system_root / "example"
    _write_skill(skill_directory, "example", "系统 Skill", "按需加载的正文")
    _write_skill(system_root / "disabled", "disabled", "禁用 Skill")
    config_path = tmp_path / "config" / "skills.json"
    config_path.parent.mkdir()
    config_path.write_text('{"enabled_skills":["example"]}', encoding="utf-8")
    monkeypatch.setattr("app.service.configuration.skill_catalog.system_skills_dir", lambda: system_root)
    monkeypatch.setattr(
        "app.service.configuration.skill_configuration.system_skills_config_file",
        lambda: config_path,
    )
    tool = SkillTool()

    listing = tool.execute(action="list")
    loaded = tool.execute(action="load", skill_name="example")
    disabled = tool.execute(action="load", skill_name="disabled")

    assert listing.status == "success"
    assert json.loads(listing.content) == [{"name": "example", "description": "系统 Skill"}]
    assert loaded.status == "success"
    assert loaded.content == "按需加载的正文\n"
    assert disabled.status == "error"


def test_missing_skill_configuration_enables_all_installed_skills(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.service.configuration.skill_configuration.system_skills_config_file",
        lambda: tmp_path / "missing-skills.json",
    )

    assert read_skill_configuration().is_enabled("newly-installed")
