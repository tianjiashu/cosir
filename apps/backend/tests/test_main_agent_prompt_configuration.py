"""主 Agent 系统提示词配置与 Registry 注入契约测试。"""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.configuration import main_agent_prompt as main_agent_prompt_api
from app.api.schemas.request.ConfigurationDocumentUpdateRequest import (
    ConfigurationDocumentUpdateRequest,
)
from app.app import app
from app.config import configuration
from app.config.configuration import build_agent_registry
from app.config.constant import Constant
from app.config.settings import Settings
from app.core.agents import agent_profile_source
from app.core.agents.agent_profile import AgentProfileType
from app.core.agents.agent_profile_config import ensure_system_agent_config_dir
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.service.configuration.instructions.main_agent_prompt_service import (
    MainAgentPromptConfigurationError,
    MainAgentPromptConfigurationService,
)
from tests.http_test_support import TRUSTED_HEADERS


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
    monkeypatch.setattr(agent_profile_source, "system_agent_config_dir", lambda: target)

    assert ensure_system_agent_config_dir() == target
    assert target.is_dir()


def test_main_prompt_update_validates_budget_and_writes_atomically(tmp_path: Path) -> None:
    """主 Agent prompt 保存应拒绝空文案和超预算正文。"""

    target = tmp_path / ".cosir" / "main_agent_system_prompt.md"
    service = MainAgentPromptConfigurationService(
        path=target,
        root=target.parent,
    )

    base = service.read().revision
    saved = service.update("新的主 Agent 协议", base_revision=base)

    assert saved.source == "user_file"
    assert saved.revision == service.read().revision
    assert target.read_text(encoding="utf-8") == "新的主 Agent 协议"
    with pytest.raises(MainAgentPromptConfigurationError):
        service.update("   ", base_revision=saved.revision)
    with pytest.raises(MainAgentPromptConfigurationError):
        service.update(
            "x" * (Constant.SystemPrompt.AGENT_PERSONA_MAX_TOKENS * 8),
            base_revision=saved.revision,
        )
    assert target.read_text(encoding="utf-8") == "新的主 Agent 协议"


@pytest.fixture
def system_data(tmp_path: Path) -> Path:
    """主 Agent 提示词文件的位置（conftest 的 ``isolate_scope_config_dirs`` 已把它隔离到用例临时目录）。"""

    return tmp_path / "system-cosir" / "main_agent_system_prompt.md"


def _main_profile(registry: AgentProfileRegistry):
    profile = registry.resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, "main_agent")
    assert profile is not None
    return profile


def test_registry_main_agent_reads_the_prompt_file(system_data: Path) -> None:
    """主 Agent 的预设正文来自提示词文件；手动改文件，下一次解析就是新正文。"""

    system_data.parent.mkdir(parents=True, exist_ok=True)
    system_data.write_text("用户配置的主 Agent 协议", encoding="utf-8")
    registry = build_agent_registry()

    profile = _main_profile(registry)
    assert profile.system_prompt == "用户配置的主 Agent 协议"
    assert profile.agent_type is AgentProfileType.MAIN

    system_data.write_text("手动改过的协议", encoding="utf-8")
    assert _main_profile(registry).system_prompt == "手动改过的协议"


def test_registry_main_agent_follows_max_steps_setting(
    system_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """步数上限算在系统作用域的签名里：环境配置改了，下一次解析就按新值重建，正文保留。"""

    system_data.parent.mkdir(parents=True, exist_ok=True)
    system_data.write_text("当前协议", encoding="utf-8")
    monkeypatch.setattr(Settings, "MAIN_AGENT_MAX_STEPS", 17)
    registry = build_agent_registry()
    assert _main_profile(registry).max_steps == 17

    monkeypatch.setattr(Settings, "MAIN_AGENT_MAX_STEPS", 42)

    profile = _main_profile(registry)
    assert profile.max_steps == 42
    assert profile.system_prompt == "当前协议"


def test_update_api_saves_the_file_and_registry_sees_it(
    system_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PUT 只写文件；注册表按签名在下一次解析时拿到新正文，不需要另行同步。"""

    registry = build_agent_registry()
    assert _main_profile(registry).system_prompt == ""
    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", registry)

    response = asyncio.run(
        main_agent_prompt_api.update_main_agent_prompt_configuration(
            ConfigurationDocumentUpdateRequest(
                content="新协议",
                base_revision=MainAgentPromptConfigurationService().read().revision,
            )
        )
    )

    assert response.content == "新协议"
    assert system_data.read_text(encoding="utf-8") == "新协议"
    assert _main_profile(registry).system_prompt == "新协议"


def test_update_api_is_registered_as_http_endpoint(system_data: Path) -> None:
    """配置中心应把主 Agent prompt 更新暴露为标准 HTTP 接口。"""

    client = TestClient(app, headers=TRUSTED_HEADERS)
    base = client.get("/configuration/main-agent-prompt").json()["revision"]
    response = client.put(
        "/configuration/main-agent-prompt",
        json={"content": "HTTP 新协议", "base_revision": base},
    )

    assert response.status_code == 200
    assert response.json()["content"] == "HTTP 新协议"
    assert system_data.read_text(encoding="utf-8") == "HTTP 新协议"
