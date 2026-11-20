import asyncio
import json
import re
import socket
import sys
from pathlib import Path

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

from app.core.tool.handler.tool_search import ToolSearchTool
from app.core.tool.schemas import ToolExecutionContext
from app.models.external_tool import ExternalToolDefinition
from app.service.configuration.extension_scope import ExtensionScope
from app.service.configuration.mcp.configuration import (
    MCPConfiguration,
    MCPConfigurationError,
    MCPServerConfig,
    ScopedMCPServer,
    available_servers,
    visible_servers,
)
from app.service.configuration.mcp.machine_state import MCPMachineState
from app.service.configuration.skills.catalog import SkillCatalog, SkillLoadError
from app.service.configuration.skills.configuration import SkillConfiguration
from app.tools.mcp.call_result import MCPCallResult
from app.tools.mcp.client import _stdio_parameters, list_server_tools
from app.tools.mcp.connection_pool import MCPConnectionPool
from app.tools.mcp.mcp_tools import MCPExternalTools
from app.tools.tool_handler.skill_tool import SkillTool
from app.utils.path import system_cosir

_BLOCK = re.compile(r'<(mcp_result|mcp_tools) source="[^"]*" untrusted>\n(.*?)\n</\1>', re.DOTALL)


def _blocks(content: str) -> list:
    """MCP 结果里各 untrusted 块的 JSON 正文（目录每个服务一块，调用结果一块）。"""
    return [json.loads(body) for _, body in _BLOCK.findall(content)]


def _write_skill(directory, name: str, description: str, body: str = "说明正文"):
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def _system_cosir(tmp_path) -> Path:
    """conftest 把系统级 Skill 启停与 MCP 配置隔离到这里。"""

    return tmp_path / "system-cosir"


def _system_skills(tmp_path) -> Path:
    """conftest 把用户主目录隔离到 ``tmp_path / "home"``，系统级 Skill 目录在它下面。"""

    return tmp_path / "home" / ".agents" / "skills"


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
        session_id=1, workspace_id=1, workspace_root=workspace_root, step_id="step", run_id=1
    )


def _pass(server: ScopedMCPServer) -> None:
    """像配置页的连接测试那样连一次：记下测试通过与它列到的工具目录。"""
    MCPMachineState.record_pass(server, asyncio.run(list_server_tools(server)))


def _definition(workspace: Path, name: str) -> ExternalToolDefinition:
    return next(d for d in MCPExternalTools().catalog(str(workspace)) if d.name == name)


def _call(workspace: Path, name: str, arguments: dict) -> dict:
    result = MCPExternalTools().execute(_definition(workspace, name), arguments, _context(workspace))
    assert result.status == "success", result.content
    return _blocks(result.content)[0]


def test_agent_facing_tool_and_argument_descriptions_are_english():
    for tool in (ToolSearchTool(), SkillTool()):
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
    system_root = _system_skills(tmp_path)
    workspace = _workspace(tmp_path)
    _write_skill(system_root / "example", "example", "系统 Skill", "系统正文")
    _write_skill(system_root / "shared", "shared", "系统版本")
    _write_skill(workspace / ".agents" / "skills" / "shared", "shared", "工作区版本", "工作区正文")
    _write_skill(workspace / ".agents" / "skills" / "workspace-only", "workspace-only", "工作区 Skill")
    # 其他产品各自的项目目录不读。
    _write_skill(workspace / ".claude" / "skills" / "foreign", "foreign", "Claude 目录")

    visible = SkillCatalog.visible_from(workspace).list_skills()
    system_only = SkillCatalog.visible_from(None).list_skills()

    assert [
        (item.name, item.description, item.scope.kind, item.overrides) for item in visible
    ] == [
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
    directory = workspace / ".agents" / "skills" / "example"
    _write_skill(directory, "example", "测试 Skill", "运行 scripts/run.py")
    (directory / "reference.md").write_text("资源正文", encoding="utf-8")
    catalog = SkillCatalog.visible_from(workspace)

    text = catalog.load_enabled("example").for_model()

    assert str(directory.resolve()) in text
    assert text.endswith("运行 scripts/run.py\n")
    info, resource = catalog.load_resource("example", "reference.md")
    assert (info.scope.kind, resource) == ("workspace", "资源正文")


def test_skill_catalog_rejects_resource_escape(tmp_path):
    system_root = _system_skills(tmp_path)
    _write_skill(system_root / "example", "example", "测试 Skill")
    (system_root / "outside.md").write_text("外部文件", encoding="utf-8")

    with pytest.raises(SkillLoadError, match="Skill 目录内"):
        SkillCatalog.visible_from(None).load_resource("example", "../outside.md")


def test_each_scope_enable_list_only_governs_its_own_skills(tmp_path):
    workspace = _workspace(tmp_path)
    _write_skill(_system_skills(tmp_path) / "system-skill", "system-skill", "系统")
    _write_skill(workspace / ".agents" / "skills" / "local-skill", "local-skill", "工作区")
    (workspace / ".cosir" / "config").mkdir(parents=True)
    # workspace 停用名单里列出系统级 Skill 也停不了它，只停得了自己这一层的。
    (workspace / ".cosir" / "config" / "skills.json").write_text(
        '{"disabled_skills":["system-skill","local-skill"]}', encoding="utf-8"
    )

    visible = {item.name: item.enabled for item in SkillCatalog.visible_from(workspace).list_skills()}

    assert visible == {"local-skill": False, "system-skill": True}


def test_missing_skill_configuration_enables_everything(tmp_path):
    workspace = _workspace(tmp_path)

    for scope in (ExtensionScope.workspace(workspace), ExtensionScope.system()):
        assert SkillConfiguration.read(scope).skill_enabled("newly-installed")


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
    MCPMachineState.record_pass(ScopedMCPServer(scope, tested), [])
    MCPMachineState.record_pass(ScopedMCPServer(scope, disabled), [])

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
    MCPMachineState.record_pass(saved, [])

    MCPMachineState.record_pass(draft, [])
    assert MCPMachineState.read().passed(saved)
    assert MCPMachineState.revoke(draft) is True
    assert MCPMachineState.read().passed(saved)
    assert MCPMachineState.revoke(draft) is False
    assert MCPMachineState.revoke(saved) is True
    assert not MCPMachineState.read().passed(saved)


def test_mcp_test_record_is_pruned_on_save_and_dropped_with_the_workspace(tmp_path):
    workspace = _workspace(tmp_path)
    scope = ExtensionScope.workspace(workspace)
    kept = ScopedMCPServer(scope, MCPServerConfig(name="kept", command="python"))
    removed = ScopedMCPServer(scope, MCPServerConfig(name="removed", command="python"))
    stale_draft = ScopedMCPServer(scope, MCPServerConfig(name="kept", command="node"))
    for server in (kept, removed, stale_draft):
        MCPMachineState.record_pass(server, [])

    MCPMachineState.keep_only(scope, [kept])
    record = MCPMachineState.read()

    assert record.passed(kept)
    # 删掉的服务再加回同一配置、残留的草稿指纹，都要重新测试。
    assert not record.passed(removed)
    assert not record.passed(stale_draft)
    MCPMachineState.drop_scope(scope)
    assert scope.key not in MCPMachineState.read().scopes


def test_untested_server_is_neither_listed_nor_called(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    _write_mcp_servers(workspace / ".cosir", MCPServerConfig(name="one", command="python"))
    monkeypatch.setattr(
        "app.service.depends.get_mcp_connection_pool",
        lambda: pytest.fail("untested MCP server must not be discovered or called"),
    )
    source = MCPExternalTools()
    # 加载过、后来测试记录被撤销的工具：快照还在上下文里，调用时按当前配置拒绝。
    stale = ExternalToolDefinition(
        name="mcp__one__search",
        description="",
        parameters={"type": "object"},
        reference={"scope": ExtensionScope.workspace(workspace).key, "server": "one", "tool": "search"},
    )

    call = source.execute(stale, {}, _context(workspace))

    assert source.catalog(str(workspace)) == ()
    assert call.status == "error"
    assert "has not passed a connection test" in call.content


def test_mcp_configuration_rejects_duplicate_names(tmp_path):
    workspace = _workspace(tmp_path)
    _write_mcp_servers(
        workspace / ".cosir",
        MCPServerConfig(name="same", command="one"),
        MCPServerConfig(name="same", command="two"),
    )

    with pytest.raises(MCPConfigurationError, match="必须唯一"):
        MCPConfiguration.read(ExtensionScope.workspace(workspace))


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

        assert [tool.name for tool in tools] == ["echo"]
        assert json.loads(MCPCallResult.from_sdk(result).text) == {"echo": "ok"}

    asyncio.run(exercise_transport())


def test_mcp_configuration_requires_transport_specific_endpoint():
    with pytest.raises(ValueError, match="stdio MCP 服务必须配置 command"):
        MCPServerConfig(name="stdio")

    with pytest.raises(ValueError, match="HTTP MCP 服务必须配置 url"):
        MCPServerConfig(name="http", transport="streamable_http")

    config = MCPServerConfig(name="http", transport="sse", url="HTTPS://example.com/sse")
    assert config.url == "HTTPS://example.com/sse"


def test_stdio_parameters_pass_explicit_environment_and_run_where_used(tmp_path, monkeypatch):
    """MCP 子进程只接收服务专属变量（SDK 另行补入默认白名单）；在哪个工作区里用就在哪运行。"""

    monkeypatch.setenv("MODEL_PROVIDER_SECRET", "must-not-be-inherited")
    config = MCPServerConfig(name="stdio", command="mcp-server", env={"SERVER_KEY": "value"})
    workspace = _workspace(tmp_path)

    outside = ScopedMCPServer(ExtensionScope.system(), config)
    system_in_workspace = ScopedMCPServer(ExtensionScope.system(), config, used_in=workspace)
    outside_parameters = _stdio_parameters(outside.connection_config(), outside.cwd)
    workspace_parameters = _stdio_parameters(
        system_in_workspace.connection_config(), system_in_workspace.cwd
    )

    assert outside_parameters.env == {"SERVER_KEY": "value"}
    assert outside_parameters.cwd is None
    assert Path(workspace_parameters.cwd) == workspace


# 潜在缺陷：系统级服务在所有工作区共用一条连接，stdio 进程不在工作区里运行、服务状态在工作区之间串用。
def test_system_server_gets_one_connection_per_workspace_it_is_used_in(tmp_path, monkeypatch):
    first, second = _workspace(tmp_path, "first"), _workspace(tmp_path, "second")
    config = _cwd_server("system")
    _write_mcp_servers(tmp_path / "system-cosir", config)
    monkeypatch.setattr(system_cosir, "LOG_DIR", tmp_path / "logs")
    _pass(ScopedMCPServer(ExtensionScope.system(), config))
    pool = MCPConnectionPool()
    monkeypatch.setattr("app.service.depends.get_mcp_connection_pool", lambda: pool)

    try:
        cwds = [
            Path(_call(workspace, "mcp__test-server__whoami", {})["cwd"]).resolve()
            for workspace in (first, second)
        ]
        connection_count = len(pool._connections)
    finally:
        pool.close()

    assert cwds == [first.resolve(), second.resolve()]
    assert connection_count == 2


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


_ROOTS_SERVER_CODE = (
    "from mcp.server.fastmcp import Context, FastMCP\n"
    "server = FastMCP('roots-server')\n"
    "@server.tool()\n"
    "async def roots(ctx: Context) -> dict:\n"
    "    try:\n"
    "        result = await ctx.session.list_roots()\n"
    "    except Exception as exc:\n"
    "        return {'error': type(exc).__name__}\n"
    "    return {'roots': [str(root.uri) for root in result.roots]}\n"
    "server.run(transport='stdio')\n"
)


# 潜在缺陷：filesystem、git 这类服务不知道当前工作区，只能在启动参数里写死路径。
def test_servers_used_in_a_workspace_are_told_its_root(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    config = MCPServerConfig(name="roots-server", command=sys.executable, args=["-c", _ROOTS_SERVER_CODE])
    monkeypatch.setattr(system_cosir, "LOG_DIR", tmp_path / "logs")
    pool = MCPConnectionPool()

    def ask(server: ScopedMCPServer) -> dict:
        result = pool.submit(pool.call_tool(server, "roots", {})).result(timeout=30)
        return json.loads(MCPCallResult.from_sdk(result).text)

    try:
        inside = ask(ScopedMCPServer(ExtensionScope.system(), config, used_in=workspace))
        outside = ask(ScopedMCPServer(ExtensionScope.system(), config))
    finally:
        pool.close()

    assert inside == {"roots": [workspace.resolve().as_uri()]}
    # 不在任何工作区里（系统级配置页）时不声明 roots 能力。
    assert "error" in outside


def test_mcp_tool_keeps_same_named_workspace_servers_apart(tmp_path, monkeypatch):
    """两个工作区的同名服务各自连接、各在自己的工作区运行，互不顶替。"""

    first, second = _workspace(tmp_path, "first"), _workspace(tmp_path, "second")
    monkeypatch.setattr(system_cosir, "LOG_DIR", tmp_path / "logs")
    for workspace, marker in ((first, "first"), (second, "second")):
        config = _cwd_server(marker)
        _write_mcp_servers(workspace / ".cosir", config)
        _pass(ScopedMCPServer(ExtensionScope.workspace(workspace), config))
    pool = MCPConnectionPool()
    monkeypatch.setattr("app.service.depends.get_mcp_connection_pool", lambda: pool)
    name = "mcp__test-server__whoami"

    try:
        results = [_call(first, name, {}), _call(second, name, {}), _call(first, name, {})]
        connection_count = len(pool._connections)
    finally:
        pool.close()

    assert _definition(first, name).reference["scope"] == ExtensionScope.workspace(first).key
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
    system_root = _system_skills(tmp_path)
    workspace = _workspace(tmp_path)
    _write_skill(system_root / "example", "example", "系统 Skill", "按需加载的正文")
    _write_skill(system_root / "disabled", "disabled", "禁用 Skill")
    _write_skill(workspace / ".agents" / "skills" / "local", "local", "工作区 Skill", "工作区正文")
    config_path = _system_cosir(tmp_path) / "config" / "skills.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('{"disabled_skills":["disabled"]}', encoding="utf-8")
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
    assert str((workspace / ".agents" / "skills" / "local").resolve()) in loaded.content
    assert loaded.content.endswith("工作区正文\n")
    assert disabled.status == "error"


_ECHO_SERVER_CODE = (
    "from mcp.server.fastmcp import FastMCP\n"
    "server = FastMCP('echo-server')\n"
    "calls = []\n"
    "@server.tool(description='Echo a value back. ' + 'Long explanation. ' * 40)\n"
    "def echo(value: str) -> dict:\n"
    "    calls.append(value)\n"
    "    return {'echo': value, 'calls': len(calls)}\n"
    "@server.tool(description='Count words in a text')\n"
    "def count_words(text: str) -> int:\n"
    "    return len(text.split())\n"
    "server.run(transport='stdio')\n"
)


@pytest.fixture
def echo_tool(tmp_path, monkeypatch):
    """一个已测试通过的工作区 stdio 服务（echo 与 count_words 两个工具）与它的延迟工具目录。"""

    workspace = _workspace(tmp_path)
    config = MCPServerConfig(name="echo", command=sys.executable, args=["-c", _ECHO_SERVER_CODE])
    _write_mcp_servers(workspace / ".cosir", config)
    monkeypatch.setattr(system_cosir, "LOG_DIR", tmp_path / "logs")
    _pass(ScopedMCPServer(ExtensionScope.workspace(workspace), config))
    pool = MCPConnectionPool()
    monkeypatch.setattr("app.service.depends.get_mcp_connection_pool", lambda: pool)
    yield workspace
    pool.close()


def test_catalog_comes_from_the_test_record_with_server_prefix_and_full_schema(echo_tool, monkeypatch):
    monkeypatch.setattr(
        "app.service.depends.get_mcp_connection_pool",
        lambda: pytest.fail("the catalog must not connect to the server"),
    )
    [count, echo] = MCPExternalTools().catalog(str(echo_tool))

    assert (count.name, echo.name) == ("mcp__echo__count_words", "mcp__echo__echo")
    assert count.description == "[MCP server echo] Count words in a text"
    assert echo.parameters["required"] == ["value"]
    assert echo.reference == {
        "scope": ExtensionScope.workspace(echo_tool).key,
        "server": "echo",
        "tool": "echo",
    }


# 潜在缺陷：服务运行期改了 schema，按旧快照校验通过的参数仍会发给服务。
def test_arguments_are_checked_against_the_live_schema_before_calling(echo_tool):
    source = MCPExternalTools()
    definition = _definition(echo_tool, "mcp__echo__echo")
    rejected = source.execute(definition, {}, _context(echo_tool))
    accepted = source.execute(definition, {"value": "hi"}, _context(echo_tool))

    assert rejected.status == "error"
    assert "was not sent" in rejected.content and '"required": ["value"]' in rejected.content
    # 被拒的那次没有到达服务：服务端只记到一次调用。
    assert _blocks(accepted.content) == [{"echo": "hi", "calls": 1}]
    assert accepted.tool_name == "mcp__echo__echo"
    assert accepted.display_data == {
        "kind": "mcp-tool-result",
        "server_name": "echo",
        "tool_name": "echo",
        "scope": "workspace",
        "images": [],
    }
