"""主 Agent 系统提示词配置与 Registry 注入契约测试。"""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.configuration import main_agent_prompt as main_agent_prompt_api
from app.api.schemas.request.MainAgentPromptUpdateRequest import MainAgentPromptUpdateRequest
from app.app import app
from app.config import configuration
from app.config.configuration import build_agent_registry
from app.config.constant import Constant
from app.core.agents import agent_profile_config
from app.core.agents.agent_profile import AgentProfileType
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.define_agents import main_agent
from app.service.configuration.main_agent_prompt_configuration_service import (
    MainAgentPromptConfigurationError,
    MainAgentPromptConfigurationService,
)


def test_missing_main_prompt_creates_blank_user_file(tmp_path: Path) -> None:
    """缺少用户配置时应创建空白文件，并返回空正文而非任何内置模板。"""

    target = tmp_path / ".cosir" / "main_agent_system_prompt.md"

    document = MainAgentPromptConfigurationService(
        path=target,
        root=target.parent,
    ).read()

    assert document.content == ""
    assert document.token_length == 0
    assert document.source == "user_file"
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == ""
    assert document.path == target


def test_system_agent_config_dir_is_created_even_without_packaged_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有随包分发的子 Agent JSON 时，启动初始化仍应成功创建目录。"""

    target = tmp_path / "system-agents"
    monkeypatch.setattr(agent_profile_config, "system_agent_config_dir", lambda: target)

    assert agent_profile_config.ensure_system_agent_config_dir() == target
    assert target.is_dir()


def test_main_prompt_update_validates_budget_and_writes_atomically(tmp_path: Path) -> None:
    """主 Agent prompt 保存应拒绝空文案和超预算正文。"""

    target = tmp_path / ".cosir" / "main_agent_system_prompt.md"
    service = MainAgentPromptConfigurationService(
        path=target,
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


def test_build_agent_registry_uses_configured_main_max_steps() -> None:
    """Registry 装配应把环境层传入的主 Agent 步数写入 profile。"""

    registry = build_agent_registry(main_agent_max_steps=17)
    profile = registry.resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, "main_agent")

    assert profile is not None
    assert profile.max_steps == 17


def test_replace_main_agent_profile_preserves_prompt_when_budget_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重载预算时应只替换 max_steps，并保留当前主 Agent prompt。"""

    registry = build_agent_registry("当前协议", main_agent_max_steps=17)
    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", registry)

    replacement = configuration.replace_main_agent_profile(max_steps=42)

    assert replacement.system_prompt == "当前协议"
    assert replacement.max_steps == 42
    current = registry.resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, "main_agent")
    assert current is replacement


def test_update_api_replaces_registry_profile_after_file_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PUT 配置成功后应同时返回新正文并替换进程内主 Agent profile。"""

    target = tmp_path / ".cosir" / "main_agent_system_prompt.md"
    service = MainAgentPromptConfigurationService(
        path=target,
        root=target.parent,
    )
    registry = build_agent_registry("旧协议", main_agent_max_steps=17)
    monkeypatch.setattr(
        main_agent_prompt_api,
        "MainAgentPromptConfigurationService",
        lambda: service,
    )
    monkeypatch.setattr(
        main_agent_prompt_api,
        "replace_main_agent_profile",
        lambda *, system_prompt=None, max_steps=None: registry.replace(
            AgentProfileRegistry.SYSTEM_WORKSPACE,
            main_agent(
                system_prompt=system_prompt or "",
                max_steps=profile_max_steps(registry, max_steps),
            ),
        ),
    )

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
    assert profile.max_steps == 17


def test_update_api_is_registered_as_http_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置中心应把主 Agent prompt 更新暴露为标准 HTTP 接口。"""

    target = tmp_path / ".cosir" / "main_agent_system_prompt.md"
    service = MainAgentPromptConfigurationService(
        path=target,
        root=target.parent,
    )
    registry = build_agent_registry("旧协议", main_agent_max_steps=17)
    monkeypatch.setattr(
        main_agent_prompt_api,
        "MainAgentPromptConfigurationService",
        lambda: service,
    )
    monkeypatch.setattr(
        main_agent_prompt_api,
        "replace_main_agent_profile",
        lambda *, system_prompt=None, max_steps=None: registry.replace(
            AgentProfileRegistry.SYSTEM_WORKSPACE,
            main_agent(
                system_prompt=system_prompt or "",
                max_steps=profile_max_steps(registry, max_steps),
            ),
        ),
    )

    response = TestClient(app).put(
        "/configuration/main-agent-prompt",
        json={"content": "HTTP 新协议"},
    )

    assert response.status_code == 200
    assert response.json()["content"] == "HTTP 新协议"


def profile_max_steps(registry: AgentProfileRegistry, max_steps: int | None) -> int:
    """返回测试替身应沿用的主 Agent 步数。"""

    if max_steps is not None:
        return max_steps
    profile = registry.resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, "main_agent")
    assert profile is not None
    return profile.max_steps
