"""系统配置中心 service 的安全边界与保存语义测试。"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.schemas.AgentConfigurationDocument import AgentConfigurationDocument
from app.app import app
from app.config.settings import Settings
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.define_agents import general_child_agent, main_agent
from app.service.configuration.agent_configuration_service import (
    AgentConfigurationError,
    AgentConfigurationService,
)
from app.service.configuration.environment_configuration_service import (
    EnvironmentChange,
    EnvironmentConfigurationError,
    EnvironmentConfigurationService,
)
from app.service.configuration.file_store import (
    ConfigurationFileStore,
    ConfigurationPathError,
)
from app.service.configuration.instruction_configuration_service import (
    InstructionConfigurationError,
    InstructionConfigurationService,
)


def _configuration_registry() -> AgentProfileRegistry:
    """构造包含系统内置 profile 的配置中心测试注册表。"""

    registry = AgentProfileRegistry()
    registry.register(AgentProfileRegistry.SYSTEM_WORKSPACE, general_child_agent())
    registry.register(AgentProfileRegistry.SYSTEM_WORKSPACE, main_agent())
    return registry


def _service(
    monkeypatch: pytest.MonkeyPatch,
    registry: AgentProfileRegistry,
) -> AgentConfigurationService:
    """构造使用指定注册表的 service。

    ``registry`` 不是构造参数（service 固定使用进程级注册表单例），因此测试在模块级查询上注入，
    构造调用形态与生产保持一致。
    """

    monkeypatch.setattr(
        "app.service.configuration.agent_configuration_service.get_agent_registry",
        lambda: registry,
    )
    return AgentConfigurationService()


def _document(service: AgentConfigurationService, agent_id: str) -> AgentConfigurationDocument:
    """从公开列举结果中按 ``agent_id`` 取文档；不存在时抛 ``KeyError``。

    读取单个文档不是 service 的公开能力（``list_documents`` 是唯一读入口），此处按测试需要
    在列举结果上做一次投影，保留「未注册的 agent 取不到文档」这一断言语义。
    """

    for document in service.list_documents():
        if document.agent_id == agent_id:
            return document
    raise KeyError(agent_id)


def test_configuration_file_store_writes_atomically_and_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "agents"
    root.mkdir()
    target = root / "worker.json"

    ConfigurationFileStore.write_text_atomic(target, "one", root=root)
    assert target.read_text(encoding="utf-8") == "one"
    ConfigurationFileStore.write_text_atomic(target, "two", root=root)
    assert target.read_text(encoding="utf-8") == "two"
    with pytest.raises(ConfigurationPathError):
        ConfigurationFileStore.assert_safe_child(root, tmp_path / "outside.json")


def test_agent_configuration_round_trip_preserves_model_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProviderService:
        @staticmethod
        def vaild_provider(provider_id: int | None, model_name: str | None) -> bool:
            return provider_id == 7 and model_name == "model-a"

    monkeypatch.setattr(
        "app.core.agents.agent_profile.get_provider_service",
        lambda: FakeProviderService(),
    )
    monkeypatch.setattr(
        "app.service.configuration.agent_configuration_service.system_agent_config_dir",
        lambda: tmp_path / "agents",
    )
    registry = _configuration_registry()
    service = _service(monkeypatch, registry)
    service.create_document(
        AgentConfigurationDocument(
            agent_id="reviewer",
            role="child",
            description="审查变更",
            system_prompt="只审查，不修改文件。",
            allowed_tools=["read_file"],
            provider_id=None,
            model_name=None,
            model_settings={"temperature": 0.2},
        )
    )

    loaded = _document(service, "reviewer")
    assert loaded.model_settings == {"temperature": 0.2}
    assert registry.resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, "reviewer") is not None
    listed_ids = {item.agent_id for item in service.list_documents()}
    assert "reviewer" in listed_ids
    assert "main_agent" not in listed_ids
    assert (
        json.loads((tmp_path / "agents" / "reviewer.json").read_text(encoding="utf-8"))[
            "model_name"
        ]
        is None
    )

    updated = service.update_document(
        "reviewer",
        AgentConfigurationDocument(
            agent_id="reviewer",
            role="child",
            description="审查变更",
            system_prompt="只审查，不修改文件。",
            allowed_tools=["read_file"],
            provider_id=7,
            model_name="model-a",
            model_settings={"temperature": 0.1},
        ),
    )
    assert _document(service, "reviewer").model_name == "model-a"
    assert updated.model_name == "model-a"
    assert (
        registry.resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, "reviewer").model_name == "model-a"
    )
    service.delete_document("reviewer")
    assert registry.resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, "reviewer") is None


def test_builtin_agent_is_editable_but_not_deletable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """系统内置子 Agent 可写入覆盖配置，但始终不能从注册表卸载。"""

    monkeypatch.setattr(
        "app.service.configuration.agent_configuration_service.system_agent_config_dir",
        lambda: tmp_path / "agents",
    )
    registry = _configuration_registry()
    service = _service(monkeypatch, registry)
    current = _document(service, "general-assistant")
    updated = AgentConfigurationDocument(
        **{**current.__dict__, "description": "可编辑的系统通用 Agent"}
    )

    service.update_document("general-assistant", updated)

    assert (
        registry.resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, "general-assistant").description
        == "可编辑的系统通用 Agent"
    )
    assert (tmp_path / "agents" / "general-assistant.json").is_file()
    with pytest.raises(AgentConfigurationError):
        service.delete_document("general-assistant")


def test_global_instruction_enforces_token_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config.constant import Constant

    monkeypatch.setattr(Constant.SystemPrompt, "GLOBAL_INSTRUCTION_MAX_FILE_TOKENS", 4)
    service = InstructionConfigurationService(path=tmp_path / "root" / "AGENTS.md")
    monkeypatch.setattr(service, "root", tmp_path / "root")
    with pytest.raises(InstructionConfigurationError):
        service.update("超预算内容")  # 5 个 CJK 字符 ≈ 5 token > 4

    document = service.update("合规内容")
    assert document.token_length == 4
    assert (tmp_path / "root" / "AGENTS.md").read_text(encoding="utf-8") == "合规内容"


def test_environment_service_masks_secrets_and_preserves_unknown_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(
        "app.service.configuration.environment_configuration_service.system_cosir_dir",
        lambda: root,
    )
    env_path = root / ".env"
    local_path = root / ".env.local"
    env_path.write_text("DEFAULT_LANGUAGE=en\nCUSTOM=value\n", encoding="utf-8")
    local_path.write_text("LANGFUSE_SECRET_KEY=old-secret\n", encoding="utf-8")
    service = EnvironmentConfigurationService(env_path=env_path, local_path=local_path)
    monkeypatch.setattr(Settings, "DEFAULT_LANGUAGE", "process-old")

    fields = service.read()
    secret = next(field for field in fields if field["name"] == "LANGFUSE_SECRET_KEY")
    assert secret["value"] is None
    assert secret["masked"] is True

    service.update(
        {"DEFAULT_LANGUAGE": EnvironmentChange("replace", "zh")},
    )
    assert "CUSTOM=value" in env_path.read_text(encoding="utf-8")
    assert 'DEFAULT_LANGUAGE="zh"' in local_path.read_text(encoding="utf-8")
    language = next(field for field in service.read() if field["name"] == "DEFAULT_LANGUAGE")
    assert language["value"] == "process-old"
    assert language["restart_value"] == "zh"
    service.update(
        {"LANGFUSE_SECRET_KEY": EnvironmentChange("clear")},
    )
    assert (
        next(field for field in service.read() if field["name"] == "LANGFUSE_SECRET_KEY")["masked"]
        is False
    )
    with pytest.raises(EnvironmentConfigurationError):
        service.update({"NOT_ALLOWED": EnvironmentChange("replace", "x")})


def test_environment_update_can_reload_runtime_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保存环境配置时按请求选择重载当前进程配置。"""

    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(
        "app.service.configuration.environment_configuration_service.system_cosir_dir",
        lambda: root,
    )
    env_path = root / ".env"
    local_path = root / ".env.local"
    service = EnvironmentConfigurationService(env_path=env_path, local_path=local_path)
    reload_calls = 0

    def fake_reload() -> None:
        nonlocal reload_calls
        reload_calls += 1

    monkeypatch.setattr(Settings, "load", staticmethod(fake_reload))
    service.update(
        {"DEFAULT_LANGUAGE": EnvironmentChange("replace", "en")},
        reload_after_write=True,
    )

    assert reload_calls == 1
    assert 'DEFAULT_LANGUAGE="en"' in local_path.read_text(encoding="utf-8")


def test_agent_operations_require_canonical_file_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "agents"
    directory.mkdir()
    monkeypatch.setattr(
        "app.service.configuration.agent_configuration_service.system_agent_config_dir",
        lambda: directory,
    )
    path = directory / "legacy-name.json"
    path.write_text(
        json.dumps(
            {
                "agent_id": "reviewer",
                "role": "child",
                "description": "审查变更",
                "system_prompt": "只审查，不修改文件。",
                "allowed_tools": ["read_file"],
                "max_steps": 100,
                "provider_id": None,
                "model_name": None,
                "model_settings": {},
            }
        ),
        encoding="utf-8",
    )
    service = _service(monkeypatch, _configuration_registry())
    assert all(item.file_name != "legacy-name.json" for item in service.list_documents())
    with pytest.raises(KeyError):
        _document(service, "reviewer")
    with pytest.raises(KeyError):
        service.delete_document("reviewer")
    assert path.exists()


def test_agent_configuration_lists_only_registered_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "agents"
    directory.mkdir()
    monkeypatch.setattr(
        "app.service.configuration.agent_configuration_service.system_agent_config_dir",
        lambda: directory,
    )
    (directory / "broken.json").write_text("{broken", encoding="utf-8")
    (directory / "main_agent.json").write_text(
        json.dumps(
            {
                "agent_id": "main_agent",
                "role": "main",
                "description": "重复内置 ID",
                "system_prompt": "x",
                "allowed_tools": ["read_file"],
                "max_steps": 100,
                "model_settings": {},
            }
        ),
        encoding="utf-8",
    )
    service = _service(monkeypatch, _configuration_registry())
    with pytest.raises(AgentConfigurationError):
        service.create_document(
            AgentConfigurationDocument(
                agent_id="general-assistant",
                role="child",
                description="重复内置 ID",
                system_prompt="x",
                allowed_tools=["read_file"],
            )
        )
    listed_ids = {item.agent_id for item in service.list_documents()}
    assert listed_ids == {"general-assistant"}
    built_in = _document(service, "general-assistant")
    # 代码内置 Agent 只能改默认值，不能改名或删除：``_is_builtin`` 判定后两者均为 False。
    assert built_in.editable is False
    assert built_in.deletable is False
    assert built_in.source == "builtin"


def test_settings_preserves_file_source_across_repeated_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("DEFAULT_LANGUAGE=zh\n", encoding="utf-8")
    monkeypatch.delenv("DEFAULT_LANGUAGE", raising=False)
    monkeypatch.setattr("app.config.settings.paths.env_files", lambda: [env_path])
    monkeypatch.setattr("app.config.settings.paths.SYSTEM_COSIR_DIR", tmp_path)
    monkeypatch.setattr(Settings, "_LOADED_ENV_VALUES", {})
    Settings._load_local_env()
    Settings._load_local_env()
    assert Settings.is_file_loaded_value("DEFAULT_LANGUAGE") is True


def test_environment_configuration_schema_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(
        "app.service.configuration.environment_configuration_service.system_cosir_dir",
        lambda: root,
    )
    service = EnvironmentConfigurationService(
        env_path=root / ".env",
        local_path=root / ".env.local",
    )
    service.update({"DEFAULT_LANGUAGE": EnvironmentChange("replace", "en")})
    service.update({"DEFAULT_LANGUAGE": EnvironmentChange("replace", "zh")})

    with TestClient(app) as client:
        response = client.post(
            "/configuration/agents",
            json={
                "agent_id": "unknown-field",
                "role": "child",
                "description": "x",
                "system_prompt": "x",
                "allowed_tool_groups": ["文件"],
                "unexpected": True,
            },
        )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "configuration_invalid"
