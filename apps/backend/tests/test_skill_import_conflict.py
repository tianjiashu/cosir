"""Skill 导入的冲突判定：已有相同的不导入；专属目录里同名的要「替换」；别处同名的要「仍然导入」。

conftest 把系统 ``.cosir`` 指到 ``tmp_path / "system-cosir"``（专属目录即其下 ``skills``），用户主目录指到
``tmp_path / "home"``（共享目录即 ``home/.agents/skills``）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.app import app
from app.service.configuration.skills.configuration import SkillConfiguration


def _skill(directory: Path, name: str, description: str) -> Path:
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n正文\n", encoding="utf-8"
    )
    return directory


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def own(tmp_path) -> Path:
    return tmp_path / "system-cosir" / "skills"


@pytest.fixture
def shared(tmp_path) -> Path:
    return tmp_path / "home" / ".agents" / "skills"


def _import(client: TestClient, source: Path, resolution: str | None = None):
    body = {"source_path": str(source), "source_type": "directory"}
    if resolution is not None:
        body["resolution"] = resolution
    return client.post("/configuration/skills/import", json=body)


def test_identical_effective_skill_is_not_imported_again(client, tmp_path, own, shared) -> None:
    _skill(shared / "review", "review", "审查")
    source = _skill(tmp_path / "source" / "review", "review", "审查")

    response = _import(client, source)
    forced = _import(client, source, "override")

    assert response.status_code == 409
    conflict = response.json()["detail"]["conflict"]
    assert conflict["relation"] == "identical"
    assert conflict["existing"] == [
        {
            "scope": "system",
            "kind": "shared",
            "location": "~/.agents/skills/review",
            "description": "审查",
            "same_content": True,
        }
    ]
    # 没有可选的处理方式：带上任何选择也不导入。
    assert forced.status_code == 409
    assert not (own / "review").exists()


def test_override_needs_confirmation_and_keeps_the_other_copy(client, tmp_path, own, shared) -> None:
    _skill(shared / "review", "review", "共享版本")
    source = _skill(tmp_path / "source" / "review", "review", "cosir 版本")

    asked = _import(client, source)
    wrong = _import(client, source, "replace")
    imported = _import(client, source, "override")

    assert asked.json()["detail"]["conflict"]["relation"] == "override"
    assert wrong.status_code == 409
    assert imported.status_code == 201, imported.text
    assert (imported.json()["kind"], imported.json()["overrides"]) == ("cosir", True)
    assert "共享版本" in (shared / "review" / "SKILL.md").read_text(encoding="utf-8")


def test_replace_swaps_the_cosir_copy_even_under_another_folder_name(
    client, tmp_path, own
) -> None:
    # 文件夹名与名称不一致的旧版本：以前导入会静默失效（扫描把新的当重名跳过）。
    _skill(own / "old-folder", "review", "旧版本")
    (own.parent / "config").mkdir(parents=True)
    (own.parent / "config" / "skills.json").write_text('{"disabled_skills": ["review"]}', encoding="utf-8")
    source = _skill(tmp_path / "source" / "review", "review", "新版本")

    asked = _import(client, source)
    replaced = _import(client, source, "replace")

    assert asked.status_code == 409
    assert asked.json()["detail"]["conflict"]["relation"] == "replace"
    assert replaced.status_code == 201, replaced.text
    assert sorted(path.name for path in own.iterdir()) == ["review"]
    assert "新版本" in (own / "review" / "SKILL.md").read_text(encoding="utf-8")
    # 用户刚选了它：停用名单里的同名项一并移除。
    assert json.loads((own.parent / "config" / "skills.json").read_text()) == {"disabled_skills": []}
    listing = {item["name"]: item for item in client.get("/configuration/skills").json()}
    assert (listing["review"]["description"], listing["review"]["enabled"]) == ("新版本", True)


def test_replace_restores_the_old_copy_when_the_configuration_cannot_be_written(
    client, tmp_path, own, monkeypatch
) -> None:
    _skill(own / "review", "review", "旧版本")
    (own.parent / "config").mkdir(parents=True)
    (own.parent / "config" / "skills.json").write_text('{"disabled_skills": ["review"]}', encoding="utf-8")
    source = _skill(tmp_path / "source" / "review", "review", "新版本")

    def fail_write(self, scope):
        raise OSError("configuration is not writable")

    monkeypatch.setattr(SkillConfiguration, "write", fail_write)
    response = _import(client, source, "replace")

    assert response.status_code == 500
    assert sorted(path.name for path in own.iterdir()) == ["review"]
    assert "旧版本" in (own / "review" / "SKILL.md").read_text(encoding="utf-8")


def test_folder_taken_by_a_differently_named_skill_is_rejected(client, tmp_path, own) -> None:
    _skill(own / "review", "something-else", "占着 review 文件夹")
    source = _skill(tmp_path / "source" / "review", "review", "新的")

    response = _import(client, source)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "configuration_conflict"
    assert "占着" in (own / "review" / "SKILL.md").read_text(encoding="utf-8")
