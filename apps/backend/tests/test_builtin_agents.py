"""系统内置子 Agent 的文件事实源回归测试。

覆盖改造前的缺陷（编辑内置子 Agent 后重启丢失）与新契约：装配时补齐缺失文件、不覆盖已有文件、
改坏的文件不报错也不进内存、删除被拒、删掉文件后重建回到出厂值。系统目录由 conftest 的
``isolate_scope_config_dirs`` 指到用例临时目录。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config.configuration import build_agent_registry
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.service.configuration.agents.builtin_agents import (
    BUILTIN_CHILD_AGENT_IDS,
    BUILTIN_CHILD_AGENTS,
)
from app.service.configuration.agents.configuration import AgentConfiguration
from app.service.configuration.agents.configuration_service import (
    AgentConfigurationError,
    AgentConfigurationService,
)

SYSTEM = AgentProfileRegistry.SYSTEM_WORKSPACE


def _system_directory(registry: AgentProfileRegistry) -> Path:
    return registry.source.directory(SYSTEM)


def _service(monkeypatch: pytest.MonkeyPatch, registry: AgentProfileRegistry, workspace: Path | None = None):
    monkeypatch.setattr(
        "app.service.configuration.agents.configuration_service.get_agent_registry",
        lambda: registry,
    )
    return AgentConfigurationService(workspace_root=workspace)


def test_first_build_writes_factory_files_and_loads_them() -> None:
    registry = build_agent_registry()
    directory = _system_directory(registry)

    for factory in BUILTIN_CHILD_AGENTS:
        path = directory / f"{factory.agent_id}.json"
        assert json.loads(path.read_text(encoding="utf-8")) == factory.to_json_document()
        loaded = registry.resolve(SYSTEM, factory.agent_id)
        assert loaded is not None and loaded.system_prompt == factory.system_prompt


def test_edit_survives_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    """改造前的缺陷：代码定义先注册，同名文件被跳过，重启后编辑丢失。"""

    service = _service(monkeypatch, build_agent_registry())
    current = next(entry for entry in service.list_entries() if entry.configuration.agent_id == "general-assistant")
    service.update(
        "general-assistant",
        AgentConfiguration(**{**current.configuration.__dict__, "description": "用户改过的说明"}),
    )

    rebuilt = build_agent_registry()
    assert rebuilt.resolve(SYSTEM, "general-assistant").description == "用户改过的说明"


def test_broken_file_is_kept_skipped_and_does_not_fail(caplog: pytest.LogCaptureFixture) -> None:
    directory = _system_directory(build_agent_registry())
    broken = directory / "general-assistant.json"
    broken.write_text("{broken", encoding="utf-8")

    registry = build_agent_registry()

    assert registry.resolve(SYSTEM, "general-assistant") is None
    assert registry.resolve(SYSTEM, "only_read_child_agent") is not None
    assert broken.read_text(encoding="utf-8") == "{broken"


def test_deleted_file_is_regenerated_on_rebuild() -> None:
    directory = _system_directory(build_agent_registry())
    (directory / "only_read_child_agent.json").unlink()

    registry = build_agent_registry()

    assert (directory / "only_read_child_agent.json").is_file()
    assert registry.resolve(SYSTEM, "only_read_child_agent") is not None


def test_builtin_agents_cannot_be_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = build_agent_registry()
    service = _service(monkeypatch, registry)

    for agent_id in BUILTIN_CHILD_AGENT_IDS:
        with pytest.raises(AgentConfigurationError):
            service.delete(agent_id)
        assert (_system_directory(registry) / f"{agent_id}.json").is_file()
    assert all(entry.builtin for entry in service.list_entries())


def test_workspace_cannot_shadow_builtin_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """工作区与系统作用域的 agent_id 冲突照旧拒绝：内置子 Agent 不会被工作区同名文件顶替。"""

    registry = build_agent_registry()
    service = _service(monkeypatch, registry, workspace=tmp_path / "workspace")
    factory = next(agent for agent in BUILTIN_CHILD_AGENTS if agent.agent_id == "general-assistant")

    with pytest.raises(AgentConfigurationError):
        service.create(AgentConfiguration(**{**factory.__dict__, "role": "工作区覆盖"}))
    assert registry.resolve(tmp_path / "workspace", "general-assistant").role == factory.role
