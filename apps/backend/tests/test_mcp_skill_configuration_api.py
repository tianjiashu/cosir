from __future__ import annotations

import asyncio
import io
import zipfile

from fastapi.testclient import TestClient

from app.app import app
from app.service.configuration import mcp_configuration_service, skill_configuration_service
from app.service.mcp import mcp_client
from app.service.skills import skill_catalog, skill_configuration


def _write_skill(root, name: str) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} description\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def test_mcp_configuration_can_be_read_and_replaced(tmp_path, monkeypatch) -> None:
    path = tmp_path / "mcp_servers.json"
    monkeypatch.setattr(mcp_configuration_service, "system_mcp_servers_file", lambda: path)
    monkeypatch.setattr(mcp_configuration_service, "system_cosir_dir", lambda: tmp_path)
    monkeypatch.setattr(mcp_client, "system_mcp_servers_file", lambda: path)
    client = TestClient(app)

    assert client.get("/configuration/mcp-servers").json() == {"servers": []}
    response = client.put(
        "/configuration/mcp-servers",
        json={
            "servers": [
                {
                    "name": "local-search",
                    "command": "python",
                    "args": ["server.py"],
                    "env": {"MODE": "local"},
                    "enabled": True,
                }
            ]
        },
    )

    assert response.status_code == 200
    assert client.get("/configuration/mcp-servers").json() == response.json()
    assert response.json()["servers"][0]["tested"] is False
    assert path.exists()

    conflict = client.put(
        "/configuration/mcp-servers",
        json={"servers": [response.json()["servers"][0], response.json()["servers"][0]]},
    )
    assert conflict.status_code == 400


def test_mcp_test_returns_discovered_tools(monkeypatch) -> None:
    async def list_tools(server):
        assert server.name == "local-search"
        return [{"name": "search", "description": "Search", "input_schema": {"type": "object"}}]

    monkeypatch.setattr(mcp_configuration_service, "list_server_tools", list_tools)

    response = TestClient(app).post(
        "/configuration/mcp-servers/test",
        json={"name": "local-search", "command": "python", "args": [], "env": {}, "enabled": True},
    )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "tools": [{"name": "search", "description": "Search", "input_schema": {"type": "object"}}],
        "error": None,
        "agent_available": False,
    }


def test_mcp_server_becomes_agent_available_only_after_saved_config_lists_tools(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "mcp_servers.json"
    monkeypatch.setattr(mcp_configuration_service, "system_mcp_servers_file", lambda: path)
    monkeypatch.setattr(mcp_configuration_service, "system_cosir_dir", lambda: tmp_path)
    monkeypatch.setattr(mcp_client, "system_mcp_servers_file", lambda: path)

    result = {"fail": False}

    async def list_tools(server):
        if result["fail"]:
            raise RuntimeError("server stopped")
        return [{"name": "search", "description": "Search", "input_schema": {"type": "object"}}]

    monkeypatch.setattr(mcp_configuration_service, "list_server_tools", list_tools)
    client = TestClient(app)
    config = {"name": "local-search", "command": "python", "args": [], "env": {}, "enabled": True}
    client.put("/configuration/mcp-servers", json={"servers": [config]})

    test_response = client.post("/configuration/mcp-servers/test", json=config)

    assert test_response.json()["agent_available"] is True
    assert client.get("/configuration/mcp-servers").json()["servers"][0]["tested"] is True

    result["fail"] = True
    failed_test = client.post("/configuration/mcp-servers/test", json=config)

    assert failed_test.json()["success"] is False
    assert client.get("/configuration/mcp-servers").json()["servers"][0]["tested"] is False

    edited = {**config, "args": ["different.py"]}
    saved = client.put("/configuration/mcp-servers", json={"servers": [edited]})
    assert saved.json()["servers"][0]["tested"] is False


def test_mcp_test_returns_timeout_result(monkeypatch) -> None:
    async def list_tools(server):
        await asyncio.sleep(0.02)
        return []

    monkeypatch.setattr(mcp_configuration_service, "MCP_TEST_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(mcp_configuration_service, "list_server_tools", list_tools)

    response = TestClient(app).post(
        "/configuration/mcp-servers/test",
        json={"name": "local-search", "command": "python", "args": [], "env": {}, "enabled": True},
    )

    assert response.status_code == 200
    assert response.json() == {
        "success": False,
        "tools": [],
        "error": "连接测试超时",
        "agent_available": False,
    }


def test_invalid_utf8_mcp_configuration_is_a_client_error(tmp_path, monkeypatch) -> None:
    path = tmp_path / "mcp_servers.json"
    path.write_bytes(b"\xff")
    monkeypatch.setattr(mcp_configuration_service, "system_mcp_servers_file", lambda: path)
    monkeypatch.setattr(mcp_client, "system_mcp_servers_file", lambda: path)

    response = TestClient(app).get("/configuration/mcp-servers")

    assert response.status_code == 400


def test_skills_can_be_listed_previewed_toggled_imported_and_deleted(tmp_path, monkeypatch) -> None:
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    config_path = tmp_path / "config" / "skills.json"
    _patch_skill_paths(monkeypatch, skills_root, config_path, tmp_path)
    _write_skill(skills_root, "existing")
    client = TestClient(app)

    skills = client.get("/configuration/skills").json()
    assert skills == [{"name": "existing", "description": "existing description", "enabled": True}]
    assert client.get("/configuration/skills/existing").json()["content"].startswith("# existing")

    response = client.put("/configuration/skills/enabled", json={"enabled_skills": []})
    assert response.status_code == 200
    assert response.json()[0]["enabled"] is False
    disabled_document = client.get("/configuration/skills/existing")
    assert disabled_document.status_code == 200
    assert disabled_document.json()["enabled"] is False
    assert client.get("/configuration/skills/missing").status_code == 404

    response = client.post(
        "/configuration/skills/import",
        data={"paths": ["new-skill/SKILL.md", "new-skill/references/guide.md"]},
        files=[
            (
                "files",
                (
                    "SKILL.md",
                    b"---\nname: new-skill\ndescription: imported\n---\n\n# Imported",
                    "text/markdown",
                ),
            ),
            ("files", ("guide.md", b"Guide", "text/markdown")),
        ],
    )
    assert response.status_code == 201, response.text
    assert response.json() == {"name": "new-skill", "description": "imported", "enabled": True}
    assert (skills_root / "new-skill" / "references" / "guide.md").read_text() == "Guide"
    assert client.get("/configuration/skills").json()[1]["enabled"] is True

    response = client.delete("/configuration/skills/new-skill")
    assert response.status_code == 200
    assert response.json() == {"name": "new-skill", "deleted": True}
    assert not (skills_root / "new-skill").exists()
    assert client.get("/configuration/skills").json() == [
        {"name": "existing", "description": "existing description", "enabled": False}
    ]


def test_skill_import_reports_name_conflict_without_replacing_existing_skill(
    tmp_path, monkeypatch
) -> None:
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    config_path = tmp_path / "skills.json"
    _patch_skill_paths(monkeypatch, skills_root, config_path, tmp_path)
    _write_skill(skills_root, "duplicate")

    response = TestClient(app).post(
        "/configuration/skills/import",
        data={"paths": ["duplicate/SKILL.md"]},
        files={
            "files": (
                "SKILL.md",
                b"---\nname: duplicate\ndescription: replacement\n---",
                "text/markdown",
            )
        },
    )

    assert response.status_code == 409
    assert (skills_root / "duplicate" / "SKILL.md").read_text().find("duplicate description") >= 0


def test_skill_zip_import_copies_archive_contents_into_system_directory(
    tmp_path, monkeypatch
) -> None:
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    config_path = tmp_path / "skills.json"
    _patch_skill_paths(monkeypatch, skills_root, config_path, tmp_path)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("new-skill/SKILL.md", b"---\nname: new-skill\ndescription: zipped\n---\n")
        bundle.writestr("new-skill/references/guide.md", b"Guide")
        bundle.writestr("new-skill/.DS_Store", b"metadata")
        bundle.writestr("__MACOSX/new-skill/._SKILL.md", b"metadata")

    response = TestClient(app).post(
        "/configuration/skills/import",
        files={"archive": ("new-skill.zip", archive.getvalue(), "application/zip")},
    )

    assert response.status_code == 201
    assert response.json() == {"name": "new-skill", "description": "zipped", "enabled": True}
    assert (skills_root / "new-skill" / "references" / "guide.md").read_text() == "Guide"


def test_skill_zip_import_rejects_archive_path_escape(tmp_path, monkeypatch) -> None:
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    _patch_skill_paths(monkeypatch, skills_root, tmp_path / "skills.json", tmp_path)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("new-skill/SKILL.md", b"---\nname: new-skill\ndescription: zipped\n---\n")
        bundle.writestr("../outside.txt", b"outside")

    response = TestClient(app).post(
        "/configuration/skills/import",
        files={"archive": ("skill.zip", archive.getvalue(), "application/zip")},
    )

    assert response.status_code == 400
    assert not (tmp_path / "outside.txt").exists()


def test_skill_zip_import_reports_invalid_archive(tmp_path, monkeypatch) -> None:
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    _patch_skill_paths(monkeypatch, skills_root, tmp_path / "skills.json", tmp_path)

    response = TestClient(app).post(
        "/configuration/skills/import",
        files={"archive": ("broken.zip", b"not a zip", "application/zip")},
    )

    assert response.status_code == 400


def test_skill_import_rolls_back_when_enabled_configuration_cannot_be_written(
    tmp_path, monkeypatch
) -> None:
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    config_path = tmp_path / "skills.json"
    config_path.write_text('{"enabled_skills": []}', encoding="utf-8")
    _patch_skill_paths(monkeypatch, skills_root, config_path, tmp_path)

    def fail_write(self, configuration):
        raise OSError("configuration is not writable")

    monkeypatch.setattr(
        skill_configuration_service.SkillConfigurationService, "_write_configuration", fail_write
    )
    response = TestClient(app).post(
        "/configuration/skills/import",
        data={"paths": ["new-skill/SKILL.md"]},
        files={
            "files": (
                "SKILL.md",
                b"---\nname: new-skill\ndescription: imported\n---\n# New",
                "text/markdown",
            )
        },
    )

    assert response.status_code == 500
    assert not (skills_root / "new-skill").exists()


def test_skill_delete_restores_directory_when_enabled_configuration_cannot_be_written(
    tmp_path, monkeypatch
) -> None:
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    config_path = tmp_path / "skills.json"
    config_path.write_text('{"enabled_skills": ["existing"]}', encoding="utf-8")
    _patch_skill_paths(monkeypatch, skills_root, config_path, tmp_path)
    _write_skill(skills_root, "existing")

    def fail_write(self, configuration):
        raise OSError("configuration is not writable")

    monkeypatch.setattr(
        skill_configuration_service.SkillConfigurationService, "_write_configuration", fail_write
    )
    response = TestClient(app).delete("/configuration/skills/existing")

    assert response.status_code == 500
    assert (skills_root / "existing" / "SKILL.md").is_file()
    assert '"existing"' in config_path.read_text(encoding="utf-8")


def test_skill_import_rejects_paths_that_escape_the_selected_folder(tmp_path, monkeypatch) -> None:
    skills_root = tmp_path / "skills"
    _patch_skill_paths(monkeypatch, skills_root, tmp_path / "skills.json", tmp_path)

    response = TestClient(app).post(
        "/configuration/skills/import",
        data={"paths": ["new-skill/../outside.txt"]},
        files={"files": ("outside.txt", b"outside", "text/plain")},
    )

    assert response.status_code == 400
    assert not (tmp_path / "outside.txt").exists()


def _patch_skill_paths(monkeypatch, skills_root, config_path, cosir_root) -> None:
    monkeypatch.setattr(skill_catalog, "system_skills_dir", lambda: skills_root)
    monkeypatch.setattr(skill_configuration, "system_skills_config_file", lambda: config_path)
    monkeypatch.setattr(skill_configuration_service, "system_skills_dir", lambda: skills_root)
    monkeypatch.setattr(
        skill_configuration_service,
        "system_skills_config_file",
        lambda: config_path,
    )
    monkeypatch.setattr(skill_configuration_service, "system_cosir_dir", lambda: cosir_root)
