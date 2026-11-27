"""整份替换的配置写入带内容版本前置条件：外部改动后保存得到 409，不静默覆盖。

覆盖文件层（``file_store.write_text_if_revision``）与四份文本文档、子 Agent 的 HTTP 接口。系统级路径由
conftest 隔离；workspace 路由的 ``workspace_id`` 经替换后的 ``resolve_workspace_root`` 映射到临时目录。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.api.configuration import workspace as workspace_api
from app.app import app
from app.config import configuration
from app.config.configuration import build_agent_registry
from app.core.tool.schemas.tool_definition import ToolDefinition
from app.core.tool.tool_registry import ToolRegistry
from app.service.configuration import file_store
from app.service.configuration.file_store import ConfigurationRevisionConflict
from app.utils.path.system_cosir import system_instruction_file
from tests.http_test_support import TRUSTED_HEADERS

WORKSPACE_ID = 7


class _NoArgs(BaseModel):
    pass


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, headers=TRUSTED_HEADERS)


@pytest.fixture
def registries(monkeypatch: pytest.MonkeyPatch) -> None:
    """子 Agent 接口要用进程级注册表：工具组换算与 Agent 解析。"""

    # 只要一个可选的工具组；真实装配要连主库。
    tool = ToolDefinition(
        name="read_file",
        group="文件",
        description="read_file 描述",
        handler=lambda: None,
        args_model=_NoArgs,
        parameters_schema={"type": "object", "properties": {}},
    )
    monkeypatch.setattr(configuration, "_TOOL_REGISTRY", ToolRegistry([tool]))
    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", build_agent_registry())


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()

    def resolve(workspace_id: int) -> Path:
        if workspace_id != WORKSPACE_ID:
            raise KeyError(workspace_id)
        return root

    monkeypatch.setattr(workspace_api, "resolve_workspace_root", resolve)
    return root


# ---- 文件层 ----


def test_write_requires_matching_revision(tmp_path: Path) -> None:
    root = tmp_path / "root"
    target = root / "doc.md"

    first = file_store.write_text_if_revision(target, "一", root=root, base_revision=None)
    assert first == file_store.file_revision(target, root=root)

    # 「期望不存在」也是前置条件：文件已在就冲突。
    with pytest.raises(ConfigurationRevisionConflict) as absent:
        file_store.write_text_if_revision(target, "二", root=root, base_revision=None)
    assert absent.value.current_revision == first

    target.write_text("外部改动", encoding="utf-8")
    with pytest.raises(ConfigurationRevisionConflict) as stale:
        file_store.write_text_if_revision(target, "二", root=root, base_revision=first)
    assert stale.value.current_revision == file_store.file_revision(target, root=root)
    assert target.read_text(encoding="utf-8") == "外部改动"

    second = file_store.write_text_if_revision(
        target, "二", root=root, base_revision=stale.value.current_revision
    )
    assert target.read_text(encoding="utf-8") == "二"
    assert second == file_store.file_revision(target, root=root)


def test_revision_tracks_content_not_timestamps(tmp_path: Path) -> None:
    """外部写回完全相同的内容不算冲突；版本按原始字节算，CRLF 文件读出的正文归一为 LF。"""

    root = tmp_path / "root"
    target = root / "doc.md"
    base = file_store.write_text_if_revision(target, "同样", root=root, base_revision=None)
    target.write_text("同样", encoding="utf-8")
    file_store.write_text_if_revision(target, "新的", root=root, base_revision=base)

    target.write_bytes(b"a\r\nb")
    text = file_store.read_revised_text(target, root=root)
    assert text.content == "a\nb"
    assert text.revision == file_store.file_revision(target, root=root)


# ---- 文本文档 HTTP ----

_DOCUMENTS = [
    ("/configuration/global-instructions", lambda tmp, ws: system_instruction_file()),
    (
        "/configuration/main-agent-prompt",
        lambda tmp, ws: tmp / "system-cosir" / "main_agent_system_prompt.md",
    ),
    (f"/workspaces/{WORKSPACE_ID}/configuration/instructions", lambda tmp, ws: ws / "AGENTS.md"),
    (
        f"/workspaces/{WORKSPACE_ID}/configuration/fileignore",
        lambda tmp, ws: ws / ".cosir" / ".fileignore",
    ),
]


@pytest.mark.parametrize(("path", "file_of"), _DOCUMENTS, ids=[item[0] for item in _DOCUMENTS])
def test_document_save_after_external_change_conflicts(
    client: TestClient, workspace: Path, tmp_path: Path, path: str, file_of
) -> None:
    target: Path = file_of(tmp_path, workspace)
    loaded = client.get(path).json()
    base = loaded["revision"]

    saved = client.put(path, json={"content": "mine-1\n", "base_revision": base})
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == client.get(path).json()["revision"]

    target.write_text("external\n", encoding="utf-8")
    stale = client.put(
        path, json={"content": "mine-2\n", "base_revision": saved.json()["revision"]}
    )
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["code"] == "configuration_revision_conflict"
    assert detail["current_revision"] == client.get(path).json()["revision"]
    assert target.read_text(encoding="utf-8") == "external\n"

    # 用户选「覆盖」：以冲突带回的当前版本为基准再保存。
    overwritten = client.put(
        path, json={"content": "mine-2\n", "base_revision": detail["current_revision"]}
    )
    assert overwritten.status_code == 200
    assert target.read_text(encoding="utf-8") == "mine-2\n"


def test_document_save_requires_base_revision(client: TestClient) -> None:
    response = client.put("/configuration/global-instructions", json={"content": "x"})
    assert response.status_code == 400


def test_workspace_instruction_moved_effective_file_conflicts(
    client: TestClient, workspace: Path
) -> None:
    """编辑时有效的是嵌套的 AGENTS.md；期间根目录出现一份更优先的，保存不能把草稿写进新文件。"""

    nested = workspace / "docs" / "AGENTS.md"
    nested.parent.mkdir()
    nested.write_text("nested", encoding="utf-8")
    path = f"/workspaces/{WORKSPACE_ID}/configuration/instructions"
    base = client.get(path).json()["revision"]

    (workspace / "AGENTS.md").write_text("root", encoding="utf-8")
    response = client.put(path, json={"content": "mine", "base_revision": base})

    assert response.status_code == 409
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == "root"
    assert nested.read_text(encoding="utf-8") == "nested"


# ---- 子 Agent HTTP ----


def _agent(**overrides) -> dict:
    return {
        "agent_id": "reviewer",
        "role": "child",
        "description": "审查变更",
        "system_prompt": "只审查。",
        "allowed_tool_groups": ["文件"],
        "max_steps": 10,
        "model_config_id": None,
        "model_settings": {},
        **overrides,
    }


@pytest.mark.usefixtures("registries")
def test_agent_update_after_external_change_conflicts(client: TestClient, tmp_path: Path) -> None:
    created = client.post("/configuration/agents", json=_agent())
    assert created.status_code == 200, created.text
    revision = created.json()["revision"]
    listed = next(
        a for a in client.get("/configuration/agents").json() if a["agent_id"] == "reviewer"
    )
    assert listed["revision"] == revision

    # 工作台草稿首存后接着改：用新建返回的版本更新。
    updated = client.put(
        "/configuration/agents/reviewer",
        json={**_agent(description="第二版"), "base_revision": revision},
    )
    assert updated.status_code == 200, updated.text

    target = tmp_path / "system-agents" / "reviewer.json"
    target.write_text(
        target.read_text(encoding="utf-8").replace("第二版", "手改"), encoding="utf-8"
    )
    stale = client.put(
        "/configuration/agents/reviewer",
        json={**_agent(description="第三版"), "base_revision": updated.json()["revision"]},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "configuration_revision_conflict"
    assert "手改" in target.read_text(encoding="utf-8")


@pytest.mark.usefixtures("registries")
def test_agent_update_requires_base_revision(client: TestClient) -> None:
    response = client.put(
        "/configuration/agents/general-purpose", json=_agent(agent_id="general-purpose")
    )
    assert response.status_code == 400
