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
from app.service.configuration import mcp_configuration_service, skill_configuration_service

_SEARCH_TOOLS = [{"name": "search", "description": "Search", "input_schema": {"type": "object"}}]


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

    async def list_tools(server):
        if state["fail"]:
            raise RuntimeError("server stopped")
        return _SEARCH_TOOLS

    monkeypatch.setattr(mcp_configuration_service, "list_server_tools", list_tools)
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
    assert (server["tested"], server["scope"], server["overrides"]) == (False, "system", False)
    saved = json.loads((system_cosir / "config" / "mcp_servers.json").read_text(encoding="utf-8"))
    assert set(saved) == {"servers"}

    conflict = client.put("/configuration/mcp-servers", json={"servers": [_server(), _server()]})
    assert conflict.status_code == 400


def test_mcp_draft_test_counts_once_the_same_config_is_saved(client, tools_ok) -> None:
    """测试草稿成功即记录：保存同一配置后立即可用，不必再测一次；保存不同配置则不可用。"""

    config = _server()
    draft_test = client.post("/configuration/mcp-servers/test", json=config)

    assert draft_test.json() == {
        "success": True,
        "tools": _SEARCH_TOOLS,
        "error": None,
        "agent_available": False,
    }
    saved = client.put("/configuration/mcp-servers", json={"servers": [config]})
    assert saved.json()["servers"][0]["tested"] is True

    edited = client.put(
        "/configuration/mcp-servers", json={"servers": [{**config, "args": ["other.py"]}]}
    )
    assert edited.json()["servers"][0]["tested"] is False


def test_testing_an_edited_draft_does_not_revoke_the_saved_server(client, tools_ok) -> None:
    """编辑已测试的服务、测了新草稿后取消：已保存的服务仍然可用（回归：草稿曾顶掉已保存记录）。"""

    config = _server()
    client.put("/configuration/mcp-servers", json={"servers": [config]})
    client.post("/configuration/mcp-servers/test", json=config)

    draft = client.post("/configuration/mcp-servers/test", json={**config, "args": ["draft.py"]})

    assert draft.json()["agent_available"] is False
    assert client.get("/configuration/mcp-servers").json()["servers"][0]["tested"] is True


def test_deleted_server_must_be_retested_when_added_back(client, tools_ok) -> None:
    config = _server()
    client.put("/configuration/mcp-servers", json={"servers": [config]})
    client.post("/configuration/mcp-servers/test", json=config)

    client.put("/configuration/mcp-servers", json={"servers": []})
    readded = client.put("/configuration/mcp-servers", json={"servers": [config]})

    assert readded.json()["servers"][0]["tested"] is False


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
    assert client.get("/configuration/mcp-servers").json()["servers"][0]["tested"] is True

    tools_ok["fail"] = True
    failed = client.post("/configuration/mcp-servers/test", json=config)

    assert failed.json()["success"] is False
    assert client.get("/configuration/mcp-servers").json()["servers"][0]["tested"] is False


def test_mcp_test_returns_timeout_result(client, monkeypatch) -> None:
    async def list_tools(server):
        await asyncio.sleep(0.02)
        return []

    monkeypatch.setattr(mcp_configuration_service, "MCP_TEST_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(mcp_configuration_service, "list_server_tools", list_tools)

    response = client.post("/configuration/mcp-servers/test", json=_server())

    assert response.status_code == 200
    assert response.json() == {
        "success": False,
        "tools": [],
        "error": "连接测试超时",
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
    assert client.get("/configuration/mcp-servers").json()["servers"][0]["tested"] is False
    workspace_view = client.get("/workspaces/7/configuration/mcp-servers").json()["servers"]
    assert {item["name"]: item["tested"] for item in workspace_view} == {
        "system-only": False,
        "shared": True,
    }
    record = json.loads(
        (system_cosir / "config" / "mcp_tested_servers.json").read_text(encoding="utf-8")
    )
    assert list(record["scopes"]) != ["system"]
    assert not (workspace / ".cosir" / "config" / "mcp_tested_servers.json").exists()


def test_workspace_routes_map_unknown_workspace_to_404(client, workspace) -> None:
    assert client.get("/workspaces/8/configuration/mcp-servers").status_code == 404
    assert client.get("/workspaces/8/configuration/skills").status_code == 404


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


def test_skills_can_be_listed_previewed_toggled_imported_and_deleted(
    client, tmp_path, system_cosir
) -> None:
    skills_root = system_cosir / "skills"
    _write_skill(skills_root, "existing")

    assert client.get("/configuration/skills").json() == [
        {
            "name": "existing",
            "description": "existing description",
            "enabled": True,
            "scope": "system",
            "overrides": False,
        }
    ]
    assert client.get("/configuration/skills/existing").json()["content"].startswith("# existing")

    response = client.put("/configuration/skills/existing/enabled", json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    disabled_document = client.get("/configuration/skills/existing")
    assert disabled_document.status_code == 200
    assert disabled_document.json()["enabled"] is False
    assert client.get("/configuration/skills/missing").status_code == 404
    assert (
        client.put("/configuration/skills/missing/enabled", json={"enabled": True}).status_code
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
        "overrides": False,
    }
    assert (skills_root / "new-skill" / "references" / "guide.md").read_text() == "Guide"
    assert client.get("/configuration/skills").json()[1]["enabled"] is True
    # 暂存目录不在 skills 目录里，安装后也不残留。
    assert sorted(path.name for path in skills_root.iterdir()) == ["existing", "new-skill"]

    response = client.delete("/configuration/skills/new-skill")
    assert response.status_code == 200
    assert response.json() == {"name": "new-skill", "deleted": True}
    assert not (skills_root / "new-skill").exists()
    assert [item["enabled"] for item in client.get("/configuration/skills").json()] == [False]


def test_workspace_skills_override_system_and_writes_stay_in_scope(
    client, tmp_path, workspace, system_cosir
) -> None:
    _write_skill(system_cosir / "skills", "shared", "system version")
    _write_skill(system_cosir / "skills", "system-only")
    source_root = tmp_path / "import-source"
    _write_skill(source_root, "shared", "workspace version")

    imported = client.post(
        "/workspaces/7/configuration/skills/import",
        json={"source_path": str(source_root / "shared"), "source_type": "directory"},
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
        "/workspaces/7/configuration/skills/system-only/enabled", json={"enabled": False}
    )
    assert inherited_toggle.status_code == 400
    assert client.delete("/workspaces/7/configuration/skills/system-only").status_code == 400
    assert (system_cosir / "skills" / "system-only").is_dir()

    toggled = client.put(
        "/workspaces/7/configuration/skills/shared/enabled", json={"enabled": False}
    )
    assert toggled.json()["enabled"] is False
    assert json.loads(
        (workspace / ".cosir" / "config" / "skills.json").read_text(encoding="utf-8")
    ) == {"enabled_skills": []}
    assert not (system_cosir / "config" / "skills.json").exists()

    deleted = client.delete("/workspaces/7/configuration/skills/shared")
    assert deleted.status_code == 200
    # 删掉工作区覆盖后，系统级同名 Skill 重新可见。
    assert client.get("/workspaces/7/configuration/skills/shared").json()["description"] == (
        "system version"
    )


def test_skill_import_reports_name_conflict_without_replacing_existing_skill(
    client, tmp_path, system_cosir
) -> None:
    skills_root = system_cosir / "skills"
    _write_skill(skills_root, "duplicate")
    source_root = tmp_path / "import-source"
    _write_skill(source_root, "duplicate", "replacement")

    response = client.post(
        "/configuration/skills/import",
        json={"source_path": str(source_root / "duplicate"), "source_type": "directory"},
    )

    assert response.status_code == 409
    assert "duplicate description" in (skills_root / "duplicate" / "SKILL.md").read_text()


def test_skill_zip_import_copies_archive_contents(client, tmp_path, system_cosir) -> None:
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
    assert (system_cosir / "skills" / "new-skill" / "references" / "guide.md").read_text() == (
        "Guide"
    )


@pytest.mark.parametrize(
    "entries",
    [
        {"new-skill/SKILL.md": b"---\nname: new-skill\ndescription: zipped\n---\n", "../outside.txt": b"x"},
        {"new-skill/SKILL.md": b"---\nname: new-skill\ndescription: x\n---", "new-skill/../outside.txt": b"x"},
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
    client, tmp_path, system_cosir, monkeypatch
) -> None:
    config_path = system_cosir / "config" / "skills.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('{"enabled_skills": []}', encoding="utf-8")

    def fail_write(self, configuration):
        raise OSError("configuration is not writable")

    monkeypatch.setattr(
        skill_configuration_service.SkillConfigurationService, "_write_configuration", fail_write
    )
    source_root = tmp_path / "import-source"
    _write_skill(source_root, "new-skill")
    response = client.post(
        "/configuration/skills/import",
        json={"source_path": str(source_root / "new-skill"), "source_type": "directory"},
    )

    assert response.status_code == 500
    assert not (system_cosir / "skills" / "new-skill").exists()


def test_skill_delete_restores_directory_when_enabled_configuration_cannot_be_written(
    client, system_cosir, monkeypatch
) -> None:
    config_path = system_cosir / "config" / "skills.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('{"enabled_skills": ["existing"]}', encoding="utf-8")
    _write_skill(system_cosir / "skills", "existing")

    def fail_write(self, configuration):
        raise OSError("configuration is not writable")

    monkeypatch.setattr(
        skill_configuration_service.SkillConfigurationService, "_write_configuration", fail_write
    )
    response = client.delete("/configuration/skills/existing")

    assert response.status_code == 500
    assert (system_cosir / "skills" / "existing" / "SKILL.md").is_file()
    assert '"existing"' in config_path.read_text(encoding="utf-8")
