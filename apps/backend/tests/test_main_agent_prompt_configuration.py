"""主 Agent 系统提示词配置、Registry 注入与运行时刷新契约测试。"""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.configuration import main_agent_prompt as main_agent_prompt_api
from app.api.schemas.request.MainAgentPromptUpdateRequest import MainAgentPromptUpdateRequest
from app.app import app
from app.config.configuration import build_agent_registry
from app.config.constant import Constant
from app.core.agents import agent_profile_config
from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.context.runtime_context_manager import RuntimeContextManager
from app.service.configuration.main_agent_prompt_configuration_service import (
    MainAgentPromptConfigurationError,
    MainAgentPromptConfigurationService,
)


def test_missing_main_prompt_is_seeded_from_packaged_default(tmp_path: Path) -> None:
    """缺少用户配置时应原子安装默认模板并返回 builtin 来源。"""

    target = tmp_path / ".cosir" / "main_agent_system_prompt.md"
    default = tmp_path / "main_agent.md"
    default.write_text("默认主 Agent 协议", encoding="utf-8")

    document = MainAgentPromptConfigurationService(
        path=target,
        default_path=default,
        root=target.parent,
    ).read()

    assert document.content == "默认主 Agent 协议"
    assert document.source == "builtin_default"
    assert target.read_text(encoding="utf-8") == document.content
    assert document.path == target


def test_empty_packaged_agent_defaults_are_a_valid_empty_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有随包分发的子 Agent JSON 时，启动初始化仍应成功创建目录。"""

    target = tmp_path / "system-agents"
    monkeypatch.setattr(agent_profile_config, "system_agent_config_dir", lambda: target)
    monkeypatch.setattr(agent_profile_config, "_DEFAULTS_DIR", tmp_path / "missing-defaults")

    assert agent_profile_config.initialize_system_agent_defaults() == target
    assert (target / agent_profile_config._DEFAULTS_MARKER).is_file()


def test_main_prompt_update_validates_budget_and_writes_atomically(tmp_path: Path) -> None:
    """主 Agent prompt 保存应拒绝空文案和超预算正文。"""

    target = tmp_path / ".cosir" / "main_agent_system_prompt.md"
    default = tmp_path / "main_agent.md"
    default.write_text("默认协议", encoding="utf-8")
    service = MainAgentPromptConfigurationService(
        path=target,
        default_path=default,
        root=target.parent,
    )

    saved = service.update("新的主 Agent 协议")

    assert saved.source == "user_file"
    assert target.read_text(encoding="utf-8") == "新的主 Agent 协议"
    with pytest.raises(MainAgentPromptConfigurationError):
        service.update("   ")
    with pytest.raises(MainAgentPromptConfigurationError):
        service.update("x" * (Constant.SystemPrompt.AGENT_PERSONA_MAX_TOKENS * 8))
    assert target.read_text(encoding="utf-8") == "新的主 Agent 协议"


def test_build_agent_registry_uses_configured_main_prompt() -> None:
    """Registry 中的主 Agent profile 应使用装配层传入的有效 prompt。"""

    registry = build_agent_registry(main_agent_system_prompt="用户配置的主 Agent 协议")
    profile = registry.resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, "main_agent")

    assert profile is not None
    assert profile.system_prompt == "用户配置的主 Agent 协议"
    assert profile.agent_type is AgentProfileType.MAIN


def test_runtime_context_manager_detects_main_prompt_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已有 manager 应能识别 Registry 中主 Agent prompt 的变更。"""

    original = AgentProfile(
        agent_id="main_agent",
        role="main_agent",
        system_prompt="旧协议",
        allowed_tools=["read_file"],
        agent_type=AgentProfileType.MAIN,
        workflow=None,  # type: ignore[arg-type]
    )
    changed = AgentProfile(
        agent_id="main_agent",
        role="main_agent",
        system_prompt="新协议",
        allowed_tools=["read_file"],
        agent_type=AgentProfileType.MAIN,
        workflow=None,  # type: ignore[arg-type]
    )
    manager = RuntimeContextManager.__new__(RuntimeContextManager)
    manager.agent_profile = original
    manager.workspace_root = "/workspace"
    monkeypatch.setattr(
        "app.core.context.runtime_context_manager.SystemPromptBuilder.build",
        lambda profile, _workspace_root: profile.system_prompt,
    )
    manager._system_prompt_source = manager._build_system_prompt_source(original.system_prompt)

    assert manager.matches_system_prompt_source(original, "/workspace")
    assert not manager.matches_system_prompt_source(changed, "/workspace")


def test_update_api_replaces_registry_profile_after_file_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PUT 配置成功后应同时返回新正文并替换进程内主 Agent profile。"""

    target = tmp_path / ".cosir" / "main_agent_system_prompt.md"
    default = tmp_path / "main_agent.md"
    default.write_text("默认协议", encoding="utf-8")
    service = MainAgentPromptConfigurationService(
        path=target,
        default_path=default,
        root=target.parent,
    )
    registry = build_agent_registry("旧协议")
    monkeypatch.setattr(
        main_agent_prompt_api,
        "MainAgentPromptConfigurationService",
        lambda: service,
    )
    monkeypatch.setattr(main_agent_prompt_api, "get_agent_registry", lambda: registry)

    response = asyncio.run(
        main_agent_prompt_api.update_main_agent_prompt_configuration(
            MainAgentPromptUpdateRequest(content="新协议")
        )
    )

    profile = registry.resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, "main_agent")
    assert response.content == "新协议"
    assert target.read_text(encoding="utf-8") == "新协议"
    assert profile is not None
    assert profile.system_prompt == "新协议"


def test_update_api_is_registered_as_http_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置中心应把主 Agent prompt 更新暴露为标准 HTTP 接口。"""

    target = tmp_path / ".cosir" / "main_agent_system_prompt.md"
    default = tmp_path / "main_agent.md"
    default.write_text("默认协议", encoding="utf-8")
    service = MainAgentPromptConfigurationService(
        path=target,
        default_path=default,
        root=target.parent,
    )
    registry = build_agent_registry("旧协议")
    monkeypatch.setattr(
        main_agent_prompt_api,
        "MainAgentPromptConfigurationService",
        lambda: service,
    )
    monkeypatch.setattr(main_agent_prompt_api, "get_agent_registry", lambda: registry)

    response = TestClient(app).put(
        "/configuration/main-agent-prompt",
        json={"content": "HTTP 新协议"},
    )

    assert response.status_code == 200
    assert response.json()["content"] == "HTTP 新协议"
