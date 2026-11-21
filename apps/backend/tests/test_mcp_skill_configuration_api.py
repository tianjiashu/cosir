"""系统级与 workspace 级 MCP / Skill 配置 HTTP 接口。

系统级路径由 conftest 隔离到 ``tmp_path / "system-cosir"``；workspace 路由的 ``workspace_id`` 经
替换后的 ``resolve_workspace_root`` 映射到用例临时目录（未登记的 id 抛 KeyError，与真实实现一致）。
"""

from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.configuration import extensions
from app.app import app
from app.service.configuration.mcp import configuration_service as mcp_configuration_service
from app.service.configuration.mcp.machine_state import MCPServerCatalog, MCPToolEntry
from app.service.configuration.skills.configuration import SkillConfiguration

_SEARCH_TOOLS = [MCPToolEntry(name="search", description="Search", input_schema={"type": "object"})]




def _write_skill(root: Path, name: str, description: str | None = None) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description or name + ' description'}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


@pytest.fixture
def system_cosir(tmp_path) -> Path:
    return tmp_path / "system-cosir"


@pytest.fixture
def system_cosir_skills(tmp_path) -> Path:
    """系统级 cosir 专属 Skill 目录：导入落在这里（conftest 随系统 ``.cosir`` 隔离）。"""

    return tmp_path / "system-cosir" / "skills"


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Path:
    """登记为 workspace 7 的临时工作区。"""

    root = tmp_path / "workspace"
    root.mkdir()

    def resolve(workspace_id: int) -> Path:
        if workspace_id != 7:
            raise KeyError(workspace_id)
        return root

    monkeypatch.setattr(extensions, "resolve_workspace_root", resolve)
    return root


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def tools_ok(monkeypatch) -> dict[str, bool]:
    """可切换成功 / 失败的连接测试替身。"""

    state = {"fail": False}

    async def list_tools(server, interaction=None):
        if state["fail"]:
            raise RuntimeError("server stopped")
        return MCPServerCatalog(tools=_SEARCH_TOOLS)

    monkeypatch.setattr(mcp_configuration_service, "list_server_catalog", list_tools)
    return state


def _server(name: str = "local-search", **overrides) -> dict:
    return {"name": name, "command": "python", "args": [], "env": {}, "enabled": True, **overrides}


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


def test_mcp_configuration_can_be_read_and_replaced(client, system_cosir) -> None:
    assert client.get("/configuration/mcp-servers").json() == {"servers": []}
    response = client.put(
        "/configuration/mcp-servers",
        json={"servers": [_server(args=["server.py"], env={"MODE": "local"})]},
    )

    assert response.status_code == 200
    assert client.get("/configuration/mcp-servers").json() == response.json()
    server = response.json()["servers"][0]
    assert (server["status"], server["scope"], server["overrides"]) == ("untested", "system", False)
    saved = json.loads((system_cosir / "config" / "mcp_servers.json").read_text(encoding="utf-8"))
    assert set(saved) == {"servers"}

    conflict = client.put("/configuration/mcp-servers", json={"servers": [_server(), _server()]})
    assert conflict.status_code == 400
    assert conflict.json()["detail"]["message"] == "MCP 服务名称必须唯一"


def test_hand_edited_duplicate_names_report_the_invariant(client, system_cosir) -> None:
    """手改配置文件出现重名时，报错说出被违反的规则，而不是笼统的「格式无效」。"""

    (system_cosir / "config").mkdir(parents=True)
    (system_cosir / "config" / "mcp_servers.json").write_text(
        json.dumps({"servers": [_server(), _server()]}), encoding="utf-8"
    )

    response = client.get("/configuration/mcp-servers")

    assert response.status_code == 400
    assert response.json()["detail"]["message"] == "MCP 配置文件格式无效：MCP 服务名称必须唯一"


def test_mcp_draft_test_counts_once_the_same_config_is_saved(client, tools_ok) -> None:
    """测试草稿成功即记录：保存同一配置后立即可用，不必再测一次；保存不同配置则不可用。"""

    config = _server()
    draft_test = client.post("/configuration/mcp-servers/test", json=config)

    assert draft_test.json() == {
        "status": "completed",
        "success": True,
        "tools": [tool.model_dump() for tool in _SEARCH_TOOLS],
        "error": None,
        "failure_kind": None,
        "needs_sign_in": False,
        "agent_available": False,
    }
    saved = client.put("/configuration/mcp-servers", json={"servers": [config]})
    assert saved.json()["servers"][0]["status"] == "available"

    edited = client.put(
        "/configuration/mcp-servers", json={"servers": [{**config, "args": ["other.py"]}]}
    )
    assert edited.json()["servers"][0]["status"] == "untested"


def test_testing_an_edited_draft_does_not_revoke_the_saved_server(client, tools_ok) -> None:
    """编辑已测试的服务、测了新草稿后取消：已保存的服务仍然可用（回归：草稿曾顶掉已保存记录）。"""

    config = _server()
    client.put("/configuration/mcp-servers", json={"servers": [config]})
    client.post("/configuration/mcp-servers/test", json=config)

    draft = client.post("/configuration/mcp-servers/test", json={**config, "args": ["draft.py"]})

    assert draft.json()["agent_available"] is False
    assert client.get("/configuration/mcp-servers").json()["servers"][0]["status"] == "available"


def test_deleted_server_must_be_retested_when_added_back(client, tools_ok) -> None:
    config = _server()
    client.put("/configuration/mcp-servers", json={"servers": [config]})
    client.post("/configuration/mcp-servers/test", json=config)

    client.put("/configuration/mcp-servers", json={"servers": []})
    readded = client.put("/configuration/mcp-servers", json={"servers": [config]})

    assert readded.json()["servers"][0]["status"] == "untested"


def test_successful_test_with_a_broken_saved_file_is_not_a_server_error(
    client, system_cosir, tools_ok
) -> None:
    path = system_cosir / "config" / "mcp_servers.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    response = client.post("/configuration/mcp-servers/test", json=_server())

    assert response.status_code == 200
    assert (response.json()["success"], response.json()["agent_available"]) == (True, False)


def test_mcp_saved_server_test_failure_revokes_availability(client, tools_ok) -> None:
    config = _server()
    client.put("/configuration/mcp-servers", json={"servers": [config]})

    assert client.post("/configuration/mcp-servers/test", json=config).json()["agent_available"]
    assert client.get("/configuration/mcp-servers").json()["servers"][0]["status"] == "available"

    tools_ok["fail"] = True
    failed = client.post("/configuration/mcp-servers/test", json=config)

    assert failed.json()["success"] is False
    assert client.get("/configuration/mcp-servers").json()["servers"][0]["status"] == "untested"


# B7 潜在缺陷：网络抖一下就撤销测试记录，Agent 失去这个服务、用户还得重测。
def test_a_transient_test_failure_keeps_the_server_available(client, monkeypatch) -> None:
    config = _server()
    client.put("/configuration/mcp-servers", json={"servers": [config]})

    async def reachable(server, interaction=None):
        return MCPServerCatalog(tools=_SEARCH_TOOLS)

    async def unreachable(server, interaction=None):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(mcp_configuration_service, "list_server_catalog", reachable)
    client.post("/configuration/mcp-servers/test", json=config)
    monkeypatch.setattr(mcp_configuration_service, "list_server_catalog", unreachable)
    failed = client.post("/configuration/mcp-servers/test", json=config).json()

    assert (failed["success"], failed["failure_kind"]) == (False, "unreachable")
    assert client.get("/configuration/mcp-servers").json()["servers"][0]["status"] == "available"


# B4 潜在缺陷：一个服务的地址变量展开后不合法，整个列表与保存都报 400，连改变量的卡片都看不到。
def test_an_invalid_expanded_url_only_marks_that_server(client, monkeypatch) -> None:
    monkeypatch.setenv("BAD_MCP_URL", "localhost:3000")
    broken = {"name": "remote", "transport": "streamable_http", "url": "${BAD_MCP_URL}", "enabled": True}

    saved = client.put("/configuration/mcp-servers", json={"servers": [_server(), broken]})
    listed = client.get("/configuration/mcp-servers")

    assert (saved.status_code, listed.status_code) == (200, 200)
    statuses = {item["name"]: item["status"] for item in listed.json()["servers"]}
    assert statuses == {"local-search": "untested", "remote": "invalid_configuration"}


def test_mcp_test_returns_timeout_result(client, monkeypatch) -> None:
    async def list_tools(server, interaction=None):
        await asyncio.sleep(0.02)
        return MCPServerCatalog()

    monkeypatch.setattr(mcp_configuration_service, "MCP_TEST_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(mcp_configuration_service, "list_server_catalog", list_tools)

    response = client.post("/configuration/mcp-servers/test", json=_server())

    assert response.status_code == 200
    assert response.json() == {
        "status": "completed",
        "success": False,
        "tools": [],
        "error": "连接超时（首次启动可能在下载依赖，可稍后重试）",
        "failure_kind": "timeout",
        "needs_sign_in": False,
        "agent_available": False,
    }


def test_invalid_utf8_mcp_configuration_is_a_client_error(client, system_cosir) -> None:
    path = system_cosir / "config" / "mcp_servers.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff")

    assert client.get("/configuration/mcp-servers").status_code == 400


def test_workspace_mcp_lists_inherited_servers_and_writes_only_its_own_file(
    client, workspace, system_cosir, tools_ok
) -> None:
    client.put(
        "/configuration/mcp-servers",
        json={"servers": [_server("shared"), _server("system-only")]},
    )

    saved = client.put(
        "/workspaces/7/configuration/mcp-servers",
        json={"servers": [_server("shared", command="node")]},
    )

    assert saved.status_code == 200
    assert [
        (item["name"], item["command"], item["scope"], item["overrides"])
        for item in saved.json()["servers"]
    ] == [
        ("system-only", "python", "system", False),
        ("shared", "node", "workspace", True),
    ]
    workspace_file = json.loads(
        (workspace / ".cosir" / "config" / "mcp_servers.json").read_text(encoding="utf-8")
    )
    assert [item["name"] for item in workspace_file["servers"]] == ["shared"]
    system_file = json.loads(
        (system_cosir / "config" / "mcp_servers.json").read_text(encoding="utf-8")
    )
    assert [item["command"] for item in system_file["servers"]] == ["python", "python"]

    # 工作区的测试只记在工作区作用域，系统级同名服务仍未测试。
    tested = client.post(
        "/workspaces/7/configuration/mcp-servers/test", json=_server("shared", command="node")
    )
    assert tested.json()["agent_available"] is True
    assert client.get("/configuration/mcp-servers").json()["servers"][0]["status"] == "untested"
    workspace_view = client.get("/workspaces/7/configuration/mcp-servers").json()["servers"]
    assert {item["name"]: item["status"] for item in workspace_view} == {
        "system-only": "untested",
        "shared": "available",
    }
    record = json.loads(
        (system_cosir / "config" / "mcp_machine_state.json").read_text(encoding="utf-8")
    )
    assert list(record["scopes"]) != ["system"]
    assert not (workspace / ".cosir" / "config" / "mcp_machine_state.json").exists()


def test_workspace_routes_map_unknown_workspace_to_404(client, workspace) -> None:
    assert client.get("/workspaces/8/configuration/mcp-servers").status_code == 404
    assert client.get("/workspaces/8/configuration/skills").status_code == 404


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


def test_skills_can_be_listed_previewed_toggled_and_imported(
    system_cosir_skills, client, tmp_path, system_cosir
) -> None:
    skills_root = system_cosir_skills
    _write_skill(skills_root, "existing")

    assert client.get("/configuration/skills").json() == [
        {
            "name": "existing",
            "description": "existing description",
            "enabled": True,
            "scope": "system",
            "kind": "cosir",
            "overrides": False,
            "warnings": [],
            "manual_only": False,
            "has_scripts": False,
        }
    ]
    assert client.get("/configuration/skills/existing").json()["content"].startswith("# existing")

    response = client.put(
        "/configuration/skills/enabled", json={"names": ["existing"], "enabled": False}
    )
    assert response.status_code == 200
    assert response.json() == {}
    disabled_document = client.get("/configuration/skills/existing")
    assert disabled_document.status_code == 200
    assert disabled_document.json()["enabled"] is False
    assert client.get("/configuration/skills/missing").status_code == 404
    assert (
        client.put(
            "/configuration/skills/enabled", json={"names": ["missing"], "enabled": True}
        ).status_code
        == 404
    )

    source_root = tmp_path / "import-source"
    _write_skill(source_root, "new-skill", "imported")
    (source_root / "new-skill" / "references").mkdir()
    (source_root / "new-skill" / "references" / "guide.md").write_text("Guide", encoding="utf-8")
    response = client.post(
        "/configuration/skills/import",
        json={"source_path": str(source_root / "new-skill"), "source_type": "directory"},
    )
    assert response.status_code == 201, response.text
    assert response.json() == {
        "name": "new-skill",
        "description": "imported",
        "enabled": True,
        "scope": "system",
        "kind": "cosir",
        "overrides": False,
        "warnings": [],
        "manual_only": False,
        "has_scripts": False,
    }
    assert (skills_root / "new-skill" / "references" / "guide.md").read_text() == "Guide"
    assert client.get("/configuration/skills").json()[1]["enabled"] is True
    # 暂存目录不在 skills 目录里，安装后也不残留。
    assert sorted(path.name for path in skills_root.iterdir()) == ["existing", "new-skill"]


def test_workspace_skills_override_system_and_writes_stay_in_scope(
    system_cosir_skills, client, tmp_path, workspace, system_cosir
) -> None:
    _write_skill(system_cosir_skills, "shared", "system version")
    _write_skill(system_cosir_skills, "system-only")
    source_root = tmp_path / "import-source"
    _write_skill(source_root, "shared", "workspace version")

    request = {"source_path": str(source_root / "shared"), "source_type": "directory"}
    # 系统级有同名、内容不同的：先问用户，确认「仍然导入」后才装。
    conflict = client.post("/workspaces/7/configuration/skills/import", json=request)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["conflict"]["relation"] == "override"
    imported = client.post(
        "/workspaces/7/configuration/skills/import", json={**request, "resolution": "override"}
    )

    assert imported.status_code == 201, imported.text
    assert (imported.json()["scope"], imported.json()["overrides"]) == ("workspace", True)
    assert (workspace / ".cosir" / "skills" / "shared" / "SKILL.md").is_file()
    listing = client.get("/workspaces/7/configuration/skills").json()
    assert [(item["name"], item["description"], item["scope"]) for item in listing] == [
        ("shared", "workspace version", "workspace"),
        ("system-only", "system-only description", "system"),
    ]
    assert client.get("/workspaces/7/configuration/skills/shared").json()["description"] == (
        "workspace version"
    )

    # 继承来的系统级 Skill 在工作区里只读。
    inherited_toggle = client.put(
        "/workspaces/7/configuration/skills/enabled",
        json={"names": ["system-only"], "enabled": False},
    )
    assert inherited_toggle.status_code == 400

    toggled = client.put(
        "/workspaces/7/configuration/skills/enabled", json={"names": ["shared"], "enabled": False}
    )
    assert toggled.status_code == 200
    assert json.loads(
        (workspace / ".cosir" / "config" / "skills.json").read_text(encoding="utf-8")
    ) == {"disabled_skills": ["shared"]}
    assert not (system_cosir / "config" / "skills.json").exists()


def test_skill_import_asks_before_replacing_a_same_name_cosir_skill(
    system_cosir_skills, client, tmp_path, system_cosir
) -> None:
    skills_root = system_cosir_skills
    _write_skill(skills_root, "duplicate")
    source_root = tmp_path / "import-source"
    _write_skill(source_root, "duplicate", "replacement")

    response = client.post(
        "/configuration/skills/import",
        json={"source_path": str(source_root / "duplicate"), "source_type": "directory"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["conflict"]["relation"] == "replace"
    assert "duplicate description" in (skills_root / "duplicate" / "SKILL.md").read_text()


def test_skill_zip_import_copies_archive_contents(system_cosir_skills, client, tmp_path, system_cosir) -> None:
    archive_path = tmp_path / "new-skill.zip"
    with zipfile.ZipFile(archive_path, "w") as bundle:
        bundle.writestr("new-skill/SKILL.md", b"---\nname: new-skill\ndescription: zipped\n---\n")
        bundle.writestr("new-skill/references/guide.md", b"Guide")
        bundle.writestr("new-skill/.DS_Store", b"metadata")
        bundle.writestr("__MACOSX/new-skill/._SKILL.md", b"metadata")

    response = client.post(
        "/configuration/skills/import",
        json={"source_path": str(archive_path), "source_type": "archive"},
    )

    assert response.status_code == 201
    assert response.json()["description"] == "zipped"
    assert (system_cosir_skills / "new-skill" / "references" / "guide.md").read_text() == (
        "Guide"
    )


@pytest.mark.parametrize(
    "entries",
    [
        {
            "new-skill/SKILL.md": b"---\nname: new-skill\ndescription: zipped\n---\n",
            "../outside.txt": b"x",
        },
        {
            "new-skill/SKILL.md": b"---\nname: new-skill\ndescription: x\n---",
            "new-skill/../outside.txt": b"x",
        },
    ],
)
def test_skill_zip_import_rejects_archive_path_escape(client, tmp_path, entries) -> None:
    archive_path = tmp_path / "skill.zip"
    with zipfile.ZipFile(archive_path, "w") as bundle:
        for name, content in entries.items():
            bundle.writestr(name, content)

    response = client.post(
        "/configuration/skills/import",
        json={"source_path": str(archive_path), "source_type": "archive"},
    )

    assert response.status_code == 400
    assert not (tmp_path / "outside.txt").exists()


def test_skill_zip_import_reports_invalid_archive(client, tmp_path) -> None:
    archive_path = tmp_path / "broken.zip"
    archive_path.write_bytes(b"not a zip")

    response = client.post(
        "/configuration/skills/import",
        json={"source_path": str(archive_path), "source_type": "archive"},
    )

    assert response.status_code == 400


def test_skill_import_rolls_back_when_enabled_configuration_cannot_be_written(
    system_cosir_skills, client, tmp_path, system_cosir, monkeypatch
) -> None:
    config_path = system_cosir / "config" / "skills.json"
    config_path.parent.mkdir(parents=True)
    # 停用过同名 Skill：导入要把它从停用名单里移除，于是必须写配置。
    config_path.write_text('{"disabled_skills": ["new-skill"]}', encoding="utf-8")

    def fail_write(self, scope):
        raise OSError("configuration is not writable")

    monkeypatch.setattr(SkillConfiguration, "write", fail_write)
    source_root = tmp_path / "import-source"
    _write_skill(source_root, "new-skill")
    response = client.post(
        "/configuration/skills/import",
        json={"source_path": str(source_root / "new-skill"), "source_type": "directory"},
    )

    assert response.status_code == 500
    assert not (system_cosir_skills / "new-skill").exists()


def test_authorize_route_reports_pending_authorization_and_unknown_flows_are_404(
    client, monkeypatch
) -> None:
    """登录授权要去浏览器时只返回授权信息（status 区分两种结局）；授权已不存在时等待与取消都给 404。"""

    async def pending(self, config):
        return mcp_configuration_service.PendingAuthorization("flow-1", "https://auth.example/authorize")

    monkeypatch.setattr(mcp_configuration_service.MCPConfigurationService, "authorize", pending)

    response = client.post("/configuration/mcp-servers/authorize", json=_server())

    assert response.json() == {
        "status": "authorization_required",
        "flow_id": "flow-1",
        "authorization_url": "https://auth.example/authorize",
    }
    assert client.post("/configuration/mcp-authorizations/unknown/wait").status_code == 404
    assert client.delete("/configuration/mcp-authorizations/unknown").status_code == 404
