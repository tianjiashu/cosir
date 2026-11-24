"""Skill 目录：每个作用域两种身份（cosir 专属 ``.cosir/skills``、共享 ``.agents/skills``），对 Agent 平等。

同名先按指纹去重（同一份只列一次），指纹不同才按优先级取一份：workspace 压过系统级，专属压过共享。
conftest 把用户主目录指到 ``tmp_path / "home"``（系统级共享目录即 ``home/.agents/skills``），系统 ``.cosir``
指到 ``tmp_path / "system-cosir"``（专属目录即 ``system-cosir/skills``，另放停用名单）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.configuration import extensions
from app.app import app
from app.core.agents.define_agents import main_agent
from app.core.context.prompt_sources.registry import SKILLS
from app.core.context.prompt_sources.source import SourceContext
from app.core.context.prompt_sources.sources.skills import render_skill_catalog
from app.core.tool.schemas import TOOL_SKILL, ToolExecutionContext
from app.service.configuration.skills.catalog import SkillCatalog
from app.service.configuration.skills.configuration_view import SkillCatalogView
from app.tools.tool_handler.skill_tool import SkillTool
from app.utils.path import system_cosir as paths
from app.utils.token_estimator import TokenEstimator
from tests.http_test_support import TRUSTED_HEADERS


def _write_skill(directory: Path, frontmatter: str, body: str = "正文") -> Path:
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return directory


def _skill(directory: Path, name: str | None = None, description: str = "说明") -> Path:
    return _write_skill(directory, f"name: {name or directory.name}\ndescription: {description}")


@pytest.fixture
def home(tmp_path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def system_cosir(tmp_path) -> Path:
    return tmp_path / "system-cosir"


@pytest.fixture
def system_own(system_cosir) -> Path:
    """系统级 cosir 专属 Skill 目录。"""

    return system_cosir / "skills"


@pytest.fixture
def system_shared(home) -> Path:
    """系统级共享 Skill 目录。"""

    return home / ".agents" / "skills"


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Path:
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
    return TestClient(app, headers=TRUSTED_HEADERS)


def _groups(view: dict) -> list[tuple]:
    """分组视图的紧凑形态：(作用域, 身份, [(名称, 处境)])。"""

    return [
        (
            group["scope"],
            group["kind"],
            [(skill["name"], skill["standing"]) for skill in group["skills"]],
        )
        for group in view["groups"]
    ]


def test_each_page_lists_both_directories_even_before_they_exist(
    client, system_own, workspace
) -> None:
    system = client.get("/configuration/skill-catalog").json()["groups"]
    workspace_page = client.get("/workspaces/7/configuration/skill-catalog").json()["groups"]

    # 优先级从高到低：本层在前、继承在后，同一层里专属在前。
    assert [(group["scope"], group["kind"], group["location"]) for group in system] == [
        ("system", "cosir", str(system_own)),
        ("system", "shared", "~/.agents/skills"),
    ]
    assert [(group["scope"], group["kind"], group["location"]) for group in workspace_page] == [
        ("workspace", "cosir", ".cosir/skills"),
        ("workspace", "shared", ".agents/skills"),
        ("system", "cosir", str(system_own)),
        ("system", "shared", "~/.agents/skills"),
    ]


def test_only_cosir_and_agents_directories_are_read(
    client, home, system_own, system_shared
) -> None:
    _skill(system_shared / "shared-one")
    _skill(system_own / "own-one")
    # 其他产品各自的目录不读；想用就导入或软链进来。
    _skill(home / ".claude" / "skills" / "claude-only")

    infos = {info.name: info.root.kind for info in SkillCatalog.visible_from(None).list_skills()}

    assert infos == {"own-one": "cosir", "shared-one": "shared"}
    assert _groups(client.get("/configuration/skill-catalog").json()) == [
        ("system", "cosir", [("own-one", "effective")]),
        ("system", "shared", [("shared-one", "effective")]),
    ]


def test_same_fingerprint_is_one_skill_listed_once(client, system_own, system_shared) -> None:
    linked = _skill(system_shared / "linked")
    system_own.mkdir(parents=True)
    (system_own / "linked").symlink_to(linked)
    # Windows 上 npx skills 不开开发者模式是复制安装，换行可能不同：内容相同就是同一份。
    _skill(system_shared / "copied")
    (system_own / "copied").mkdir()
    (system_own / "copied" / "SKILL.md").write_bytes(
        (system_shared / "copied" / "SKILL.md").read_bytes().replace(b"\n", b"\r\n")
    )

    infos = {info.name: info for info in SkillCatalog.visible_from(None).list_skills()}

    assert {name: (info.root.kind, info.overrides) for name, info in infos.items()} == {
        "copied": ("cosir", False),
        "linked": ("cosir", False),
    }
    assert infos["copied"].also_at == (system_shared / "copied",)
    # 同一份只在优先级最高处列出，共享分组里不再出现。
    assert _groups(client.get("/configuration/skill-catalog").json()) == [
        ("system", "cosir", [("copied", "effective"), ("linked", "effective")]),
        ("system", "shared", []),
    ]
    assert client.get("/configuration/skills/copied").json()["also_at"] == [
        "~/.agents/skills/copied"
    ]


def test_different_fingerprint_lets_cosir_win_in_the_same_scope(
    client, system_own, system_shared
) -> None:
    _skill(system_shared / "review", description="共享版本")
    _skill(system_own / "review", description="cosir 版本")

    [info] = SkillCatalog.visible_from(None).list_skills()

    assert (info.root.kind, info.description, info.overrides) == ("cosir", "cosir 版本", True)
    assert _groups(client.get("/configuration/skill-catalog").json()) == [
        ("system", "cosir", [("review", "effective")]),
        ("system", "shared", [("review", "shadowed")]),
    ]


def test_workspace_overrides_system_with_the_same_name(client, workspace, system_shared) -> None:
    _skill(system_shared / "review", description="用户级")
    _skill(system_shared / "lint", description="只在系统级")
    _skill(workspace / ".agents" / "skills" / "review", description="项目级")
    (workspace / ".agents" / "skills" / "review" / "scripts").mkdir()

    infos = {info.name: info for info in SkillCatalog.visible_from(workspace).list_skills()}

    assert set(infos) == {"review", "lint"}
    review = infos["review"]
    assert (review.scope.kind, review.description, review.overrides) == (
        "workspace",
        "项目级",
        True,
    )
    assert review.has_scripts is True
    assert infos["lint"].overrides is False
    assert _groups(client.get("/workspaces/7/configuration/skill-catalog").json()) == [
        ("workspace", "cosir", []),
        ("workspace", "shared", [("review", "effective")]),
        ("system", "cosir", []),
        ("system", "shared", [("lint", "effective"), ("review", "shadowed")]),
    ]


def test_metadata_follows_the_standard_lenient_rules(client, system_shared) -> None:
    root = system_shared
    _skill(root / "renamed", name="other-name")
    _write_skill(root / "nameless", "description: 没有 name")
    _skill(root / "Odd Name", name="Odd Name")
    _write_skill(root / "no-description", "name: no-description")
    _write_skill(root / "broken-yaml", "name: [unclosed\ndescription: x")
    # 其他工具管理的点目录、没有 SKILL.md 的分组目录都不扫。
    _skill(root / ".system" / "bundled")
    _skill(root / "group" / "nested")

    infos = {info.name: info.warnings for info in SkillCatalog.visible_from(None).list_skills()}

    assert set(infos) == {"other-name", "nameless", "Odd Name"}
    assert infos["other-name"] == ("name 与目录名「renamed」不一致",)
    assert infos["nameless"] == ("缺少 name，已按目录名使用",)
    assert infos["Odd Name"] == (
        "name 不符合标准：只能用小写字母、数字和单个连字符，最长 64 个字符",
    )
    _own, group = client.get("/configuration/skill-catalog").json()["groups"]
    assert len(group["skills"]) == 3
    assert {item["directory"]: item["reason"] for item in group["skipped"]} == {
        "broken-yaml": "SKILL.md 的 YAML frontmatter 格式无效",
        "no-description": "Skill description 不能为空",
    }
    listing = client.get("/configuration/skills").json()
    assert [item["warnings"] for item in listing if item["name"] == "other-name"] == [
        ["name 与目录名「renamed」不一致"]
    ]


def test_import_stays_strict(client, tmp_path, system_own) -> None:
    source = _skill(tmp_path / "import-source" / "renamed", name="other-name")

    response = client.post(
        "/configuration/skills/import",
        json={"source_path": str(source), "source_type": "directory"},
    )

    assert response.status_code == 400
    assert "name 与目录名" in response.json()["detail"]["message"]
    # 专属目录是 cosir 自己的，可以先建好；被拒的导入不留下任何东西（暂存点目录也清掉）。
    assert list(system_own.iterdir()) == []


def test_import_writes_only_into_the_cosir_directory(
    client, tmp_path, home, system_cosir, system_own
) -> None:
    source = _skill(tmp_path / "import-source" / "review")

    imported = client.post(
        "/configuration/skills/import",
        json={"source_path": str(source), "source_type": "directory"},
    )

    assert imported.status_code == 201, imported.text
    assert (imported.json()["kind"], imported.json()["overrides"]) == ("cosir", False)
    assert (system_own / "review" / "SKILL.md").is_file()
    # 共享目录 cosir 从不写入；暂存的点目录用完即删。
    assert not (home / ".agents").exists()
    assert sorted(path.name for path in system_own.iterdir()) == ["review"]


def test_import_into_the_real_system_cosir_passes_the_protected_paths(
    client, tmp_path, monkeypatch
) -> None:
    """回归：受保护根与系统 ``.cosir`` 是同一处时（真实运行就是这样），导入的校验仍读得到暂存的 SKILL.md。

    其他用例里两者是不同的临时目录，曾经把「暂存放在 .cosir/ 上一级会被拒读」的缺陷藏住了。
    """

    from app.service.configuration import extension_scope
    from app.service.configuration.skills import skill_root

    data_root = tmp_path / "data"
    monkeypatch.setattr(extension_scope, "system_cosir_dir", lambda: data_root / ".cosir")
    monkeypatch.setattr(
        skill_root, "system_cosir_skills_dir", lambda: data_root / ".cosir" / "skills"
    )
    source = _skill(tmp_path / "import-source" / "review")
    paths.override(DATA_DIR=data_root)
    try:
        response = client.post(
            "/configuration/skills/import",
            json={"source_path": str(source), "source_type": "directory"},
        )
    finally:
        paths.reset()

    assert response.status_code == 201, response.text
    assert sorted(path.name for path in (data_root / ".cosir" / "skills").iterdir()) == ["review"]


def test_disable_writes_only_the_cosir_preference(client, system_shared, system_cosir) -> None:
    directory = _skill(system_shared / "review")

    toggled = client.put(
        "/configuration/skills/enabled", json={"names": ["review"], "enabled": False}
    )

    assert toggled.status_code == 200
    assert [info.enabled for info in SkillCatalog.visible_from(None).list_skills()] == [False]
    assert (directory / "SKILL.md").is_file()
    assert json.loads((system_cosir / "config" / "skills.json").read_text()) == {
        "disabled_skills": ["review"],
    }


def test_skills_cannot_be_deleted_from_cosir(client, workspace, system_shared) -> None:
    """目录与 Codex 等共用、工作区目录在 git 里：cosir 只启停，不提供删除。"""

    directory = _skill(system_shared / "review")
    _skill(workspace / ".agents" / "skills" / "local")

    assert client.delete("/configuration/skills/review").status_code == 405
    assert client.delete("/workspaces/7/configuration/skills/local").status_code == 405
    assert (directory / "SKILL.md").is_file()
    assert (workspace / ".agents" / "skills" / "local" / "SKILL.md").is_file()


def test_manual_only_skills_stay_out_of_the_model_catalog_but_load(
    system_shared, workspace
) -> None:
    _write_skill(
        system_shared / "deploy",
        "name: deploy\ndescription: 发布\ndisable-model-invocation: true",
        "发布步骤",
    )
    _skill(system_shared / "review")
    context = ToolExecutionContext(
        session_id=1, workspace_id=1, workspace_root=workspace, step_id="step", run_id=1
    )

    listing = SkillTool().execute(action="list", execution_context=context)
    loaded = SkillTool().execute(action="load", skill_name="deploy", execution_context=context)
    layer = SKILLS.render(
        SKILLS.project(SourceContext.locate(str(workspace), frozenset({TOOL_SKILL}), main_agent()))
    )

    assert [item["name"] for item in json.loads(listing.content)] == ["review"]
    assert loaded.status == "success"
    assert loaded.content.endswith("发布步骤\n")
    assert "- review: " in layer
    assert "deploy" not in layer


def test_skill_linked_into_protected_cosir_data_is_skipped(tmp_path, system_shared) -> None:
    data_root = tmp_path / "data"
    secret = _skill(data_root / ".cosir" / "storage" / "leak")
    system_shared.mkdir(parents=True)
    (system_shared / "leak").symlink_to(secret)
    paths.override(DATA_DIR=data_root)
    try:
        assert SkillCatalog.visible_from(None).list_skills() == []
        _own, group = SkillCatalogView.build(None).groups
    finally:
        paths.reset()

    assert [(item.directory, item.reason) for item in group.skipped] == [
        ("leak", "Skill 文件位于受保护目录，无法读取")
    ]


def test_detail_lists_files_metadata_and_location_for_disabled_skills_too(client, home) -> None:
    directory = _write_skill(
        home / ".agents" / "skills" / "ticket",
        "name: ticket\ndescription: 工单\nlicense: MIT\nallowed-tools: Bash(python:*)\n"
        "metadata:\n  author: someone",
        "# Ticket\n\n运行 scripts/run.py",
    )
    (directory / "scripts").mkdir()
    (directory / "scripts" / "run.py").write_text("print('hi')\n", encoding="utf-8")
    (directory / "assets").mkdir()
    (directory / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0")
    (directory / ".git").mkdir()
    (directory / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    (directory / "node_modules").mkdir()
    (directory / "node_modules" / "dep.js").write_text("x", encoding="utf-8")
    (directory / ".DS_Store").write_bytes(b"\0")
    client.put("/configuration/skills/enabled", json={"names": ["ticket"], "enabled": False})

    detail = client.get("/configuration/skills/ticket").json()

    assert detail["enabled"] is False
    assert detail["content"] == "# Ticket\n\n运行 scripts/run.py\n"
    assert detail["directory"] == str(directory.resolve())
    assert detail["metadata"] == {
        "license": "MIT",
        "allowed-tools": "Bash(python:*)",
        "metadata": '{"author": "someone"}',
    }
    assert [(item["path"], item["binary"]) for item in detail["files"]] == [
        ("SKILL.md", False),
        ("assets/logo.png", True),
        ("scripts/run.py", False),
    ]
    assert detail["files_truncated"] is False


def test_file_preview_stays_inside_the_skill_directory(client, home) -> None:
    directory = _skill(home / ".agents" / "skills" / "ticket")
    (directory / "scripts").mkdir()
    (directory / "scripts" / "run.py").write_text("print('hi')\n", encoding="utf-8")
    (home / ".agents" / "skills" / "secret.txt").write_text("outside", encoding="utf-8")

    preview = client.get("/configuration/skills/ticket/file", params={"path": "scripts/run.py"})
    escape = client.get("/configuration/skills/ticket/file", params={"path": "../secret.txt"})
    missing = client.get("/configuration/skills/nope/file", params={"path": "SKILL.md"})

    assert preview.json() == {"path": "scripts/run.py", "content": "print('hi')\n"}
    assert escape.status_code == 400
    assert missing.status_code == 404


def test_batch_toggle_is_all_or_nothing(client, home, workspace) -> None:
    for name in ("lark-mail", "lark-base"):
        _skill(home / ".agents" / "skills" / name)
    _skill(workspace / ".agents" / "skills" / "local")

    rejected = client.put(
        "/workspaces/7/configuration/skills/enabled",
        json={"names": ["local", "lark-mail"], "enabled": False},
    )
    batch = client.put(
        "/configuration/skills/enabled",
        json={"names": ["lark-mail", "lark-base"], "enabled": False},
    )

    # 继承来的系统级 Skill 混在里面时整体拒绝，工作区里的那个也没被停用。
    assert rejected.status_code == 400
    assert not (workspace / ".cosir" / "config" / "skills.json").exists()
    assert batch.status_code == 200
    assert {info.name: info.enabled for info in SkillCatalog.visible_from(None).list_skills()} == {
        "lark-base": False,
        "lark-mail": False,
    }


def test_agent_catalog_size_matches_the_prompt_catalog(client, home) -> None:
    _skill(home / ".agents" / "skills" / "review", description="审查代码改动")
    _skill(home / ".agents" / "skills" / "release", description="Prepare a release")
    _skill(home / ".agents" / "skills" / "off", description="停用的不算")
    _write_skill(
        home / ".agents" / "skills" / "deploy",
        "name: deploy\ndescription: 仅手动的也不算\ndisable-model-invocation: true",
    )
    client.put("/configuration/skills/enabled", json={"names": ["off"], "enabled": False})

    view = client.get("/configuration/skill-catalog").json()

    expected = render_skill_catalog([("release", "Prepare a release"), ("review", "审查代码改动")])
    assert view["agent_skills"] == 2
    assert view["agent_tokens"] == TokenEstimator.estimate(expected)
