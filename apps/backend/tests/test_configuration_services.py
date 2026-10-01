"""系统配置中心 service 的安全边界与保存语义测试。"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.schemas.AgentConfigurationDocument import AgentConfigurationDocument
from app.api.schemas.response.EnvironmentResponse import EnvironmentResponse
from app.app import app
from app.config.settings import Settings
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.define_agents import general_child_agent, main_agent
from app.models.environment.environment_change import EnvironmentChange
from app.service.configuration.agent_configuration_service import (
    AgentConfigurationError,
    AgentConfigurationService,
)
from app.service.configuration.environment_configuration_service import (
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
    monkeypatch.setattr(
        "app.core.agents.agent_profile.get_model_config_service",
        lambda: type(
            "FakeModelConfigService",
            (),
            {
                    "get_config": staticmethod(
                        lambda config_id: type(
                            "Config",
                            (),
                            {
                                "model_name": "model-a",
                                "base_url": "https://example.test",
                                "api_key": "secret",
                                "context_window_k": 128,
                                "supports_thinking": True,
                                "supports_reasoning_effort": True,
                                "supports_image": False,
                            },
                        )()
                )
            },
        )(),
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
            model_config_id=None,
            model_settings={"temperature": 0.2},
        )
    )

    loaded = _document(service, "reviewer")
    assert loaded.model_settings == {"temperature": 0.2}
    assert registry.resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, "reviewer") is not None
    listed_ids = {item.agent_id for item in service.list_documents()}
    assert "reviewer" in listed_ids
    assert "main_agent" not in listed_ids
    assert "model_name" not in json.loads(
        (tmp_path / "agents" / "reviewer.json").read_text(encoding="utf-8")
    )

    updated = service.update_document(
        "reviewer",
        AgentConfigurationDocument(
            agent_id="reviewer",
            role="child",
            description="审查变更",
            system_prompt="只审查，不修改文件。",
            allowed_tools=["read_file"],
            model_config_id=7,
            model_settings={"temperature": 0.1},
        ),
    )
    assert _document(service, "reviewer").model_config_id == 7
    assert updated.model_config_id == 7
    assert (
        registry.resolve(
            AgentProfileRegistry.SYSTEM_WORKSPACE, "reviewer"
        ).model_settings.model_name
        == "model-a"
    )
    service.delete_document("reviewer")
    assert registry.resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, "reviewer") is None


def test_agent_create_emits_deferred_catalog_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """创建子 Agent 后发布目录变更通知。"""

    monkeypatch.setattr(
        "app.service.configuration.agent_configuration_service.system_agent_config_dir",
        lambda: tmp_path / "agents",
    )
    registry = _configuration_registry()
    service = _service(monkeypatch, registry)
    changes: list[object] = []
    monkeypatch.setattr(
        "app.service.configuration.agent_configuration_service.broadcast_agent_catalog_change",
        changes.append,
        raising=False,
    )

    service.create_document(
        AgentConfigurationDocument(
            agent_id="reviewer",
            role="child",
            description="审查变更",
            system_prompt="只审查，不修改文件。",
            allowed_tools=["read_file"],
        )
    )

    assert len(changes) == 1
    assert changes[0].action == "created"
    assert changes[0].agent_id == "reviewer"
    assert changes[0].current_description == "审查变更"


def test_agent_update_notifies_only_when_description_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """更新子 Agent 的非描述字段不通知，描述变化才通知。"""

    monkeypatch.setattr(
        "app.service.configuration.agent_configuration_service.system_agent_config_dir",
        lambda: tmp_path / "agents",
    )
    registry = _configuration_registry()
    service = _service(monkeypatch, registry)
    original = AgentConfigurationDocument(
        agent_id="reviewer",
        role="child",
        description="审查变更",
        system_prompt="只审查，不修改文件。",
        allowed_tools=["read_file"],
    )
    service.create_document(original)
    changes: list[object] = []
    monkeypatch.setattr(
        "app.service.configuration.agent_configuration_service.broadcast_agent_catalog_change",
        changes.append,
        raising=False,
    )

    service.update_document(
        "reviewer",
        AgentConfigurationDocument(
            **{**original.__dict__, "model_settings": {"temperature": 0.2}}
        ),
    )
    assert changes == []

    service.update_document(
        "reviewer",
        AgentConfigurationDocument(
            **{**original.__dict__, "description": "审查 TypeScript 变更"}
        ),
    )
    assert len(changes) == 1
    assert changes[0].action == "updated"
    assert changes[0].previous_description == "审查变更"
    assert changes[0].current_description == "审查 TypeScript 变更"


def test_agent_delete_emits_deferred_catalog_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除子 Agent 后发布目录变更通知。"""

    monkeypatch.setattr(
        "app.service.configuration.agent_configuration_service.system_agent_config_dir",
        lambda: tmp_path / "agents",
    )
    registry = _configuration_registry()
    service = _service(monkeypatch, registry)
    document = AgentConfigurationDocument(
        agent_id="reviewer",
        role="child",
        description="审查变更",
        system_prompt="只审查，不修改文件。",
        allowed_tools=["read_file"],
    )
    service.create_document(document)
    changes: list[object] = []
    monkeypatch.setattr(
        "app.service.configuration.agent_configuration_service.broadcast_agent_catalog_change",
        changes.append,
    )

    service.delete_document("reviewer")

    assert len(changes) == 1
    assert changes[0].action == "deleted"
    assert changes[0].agent_id == "reviewer"
    assert changes[0].previous_description == "审查变更"
    assert changes[0].current_description is None


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


def _bind_env_file(monkeypatch: pytest.MonkeyPatch, env_file: Path) -> None:
    """把 service 使用的 env 文件位置指向测试临时目录。

    service 不接受路径参数（位置唯一由 ``cosir_paths`` 决定），故测试改为替换本模块引用的路径
    解析函数来完成文件系统隔离。
    """

    monkeypatch.setattr(
        "app.service.configuration.environment_configuration_service.system_env_file",
        lambda: env_file,
    )


def test_environment_service_masks_secrets_and_preserves_unknown_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(
        "app.service.configuration.environment_configuration_service.system_cosir_dir",
        lambda: root,
    )
    env_file = root / ".env"
    _bind_env_file(monkeypatch, env_file)
    env_file.write_text(
        "DEFAULT_LANGUAGE=en\nCUSTOM=value\nLANGFUSE_SECRET_KEY=old-secret\n",
        encoding="utf-8",
    )
    service = EnvironmentConfigurationService()
    monkeypatch.setattr(Settings, "DEFAULT_LANGUAGE", "process-old")

    fields = service.read()
    secret = next(field for field in fields if field["name"] == "LANGFUSE_SECRET_KEY")
    assert secret["value"] is None
    assert secret["masked"] is True

    groups = service.read_grouped()
    assert [group["id"] for group in groups] == ["general", "web", "langfuse"]
    langfuse = next(group for group in groups if group["id"] == "langfuse")
    response = EnvironmentResponse(groups=groups)
    assert response.groups[0].fields[0].name == "DEFAULT_LANGUAGE"
    max_steps = next(
        field
        for group in response.groups
        for field in group.fields
        if field.name == "MAIN_AGENT_MAX_STEPS"
    )
    assert max_steps.type == "integer"
    assert max_steps.value == 300
    assert max_steps.minimum == 1
    assert [field["name"] for field in langfuse["fields"]] == [
        "LANGFUSE_ENABLED",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
    ]
    assert (
        next(field for field in langfuse["fields"] if field["name"] == "LANGFUSE_ENABLED")[
            "component"
        ]
        == "checkbox"
    )
    assert (
        next(field for field in langfuse["fields"] if field["name"] == "LANGFUSE_SECRET_KEY")[
            "component"
        ]
        == "password"
    )
    web = next(group for group in groups if group["id"] == "web")
    assert [field["name"] for field in web["fields"]] == [
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL",
    ]
    api_key = next(field for field in web["fields"] if field["name"] == "FIRECRAWL_API_KEY")
    assert api_key["component"] == "password"
    assert api_key["secret"] is True
    assert api_key["value"] is None
    api_url = next(field for field in web["fields"] if field["name"] == "FIRECRAWL_API_URL")
    assert api_url["component"] == "input"
    assert api_url["secret"] is False
    # 保存即就地重载，故契约里不再有「重启后生效」相关字段。
    assert "restart_required" not in api_url
    assert "restart_value" not in api_url

    service.update(
        {"DEFAULT_LANGUAGE": EnvironmentChange("replace", "zh")},
    )
    assert "CUSTOM=value" in env_file.read_text(encoding="utf-8")
    assert 'DEFAULT_LANGUAGE="zh"' in env_file.read_text(encoding="utf-8")
    language = next(field for field in service.read() if field["name"] == "DEFAULT_LANGUAGE")
    assert language["value"] == "process-old"
    assert language["disk_value"] == "zh"
    service.update(
        {"LANGFUSE_SECRET_KEY": EnvironmentChange("clear")},
    )
    assert (
        next(field for field in service.read() if field["name"] == "LANGFUSE_SECRET_KEY")["masked"]
        is False
    )
    with pytest.raises(EnvironmentConfigurationError):
        service.update({"NOT_ALLOWED": EnvironmentChange("replace", "x")})
    with pytest.raises(EnvironmentConfigurationError):
        service.update({"DEFAULT_LANGUAGE": EnvironmentChange("replace", "fr")})
    # Web Provider 选择是固定值，不属于可编辑白名单：当前只有 firecrawl 一个实现，
    # 显式赋值只会引入「填错名字就选不到 Provider」的失败路径。
    for backend_field in ("WEB_BACKEND", "WEB_SEARCH_BACKEND", "WEB_EXTRACT_BACKEND"):
        with pytest.raises(EnvironmentConfigurationError):
            service.update({backend_field: EnvironmentChange("replace", "firecrawl")})


def test_web_provider_selection_is_not_env_overridable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web Provider 选择是固定值：同名环境变量不得改变 ``Settings``。

    当前内置 Web Provider 只有 firecrawl 一个实现，这三项既不对外配置也无需按环境覆盖。
    本用例锁住「``Settings.load`` 不读这三个环境变量」这一事实，避免后续误加回读取后，
    与配置中心白名单对「谁能配置」产生两套口径。
    """

    # 隔离文件来源与路径重算，使本用例只验证「环境变量是否被读取」这一件事。
    monkeypatch.setattr("app.config.settings.paths.env_file", lambda: tmp_path / "absent.env")
    monkeypatch.setattr("app.config.settings.paths.reset", lambda: None)
    monkeypatch.setenv("WEB_BACKEND", "unregistered")
    monkeypatch.setenv("WEB_SEARCH_BACKEND", "unregistered")
    monkeypatch.setenv("WEB_EXTRACT_BACKEND", "unregistered")

    Settings.load()

    assert Settings.WEB_BACKEND == ""
    assert Settings.WEB_SEARCH_BACKEND == ""
    assert Settings.WEB_EXTRACT_BACKEND == ""


def test_settings_loads_main_agent_max_steps_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settings.load 应把主 Agent 步数解析为正整数，并拒绝非法值。"""

    monkeypatch.setattr("app.config.settings.paths.env_file", lambda: tmp_path / "absent.env")
    monkeypatch.setattr("app.config.settings.paths.reset", lambda: None)
    monkeypatch.setattr(Settings, "_LOADED_ENV_VALUES", {})
    monkeypatch.setattr(Settings, "MAIN_AGENT_MAX_STEPS", 300)
    monkeypatch.setenv("MAIN_AGENT_MAX_STEPS", "42")

    Settings.load()

    assert Settings.MAIN_AGENT_MAX_STEPS == 42

    monkeypatch.setenv("MAIN_AGENT_MAX_STEPS", "0")
    with pytest.raises(ValueError, match="MAIN_AGENT_MAX_STEPS"):
        Settings.load()


def test_environment_update_can_reload_runtime_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保存环境配置时按请求就地重载 Settings，并刷新主 Agent profile。"""

    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(
        "app.service.configuration.environment_configuration_service.system_cosir_dir",
        lambda: root,
    )
    env_file = root / ".env"
    _bind_env_file(monkeypatch, env_file)
    service = EnvironmentConfigurationService()
    events: list[str] = []

    monkeypatch.setattr(Settings, "load", staticmethod(lambda: events.append("settings")))
    monkeypatch.setattr(
        "app.config.configuration.replace_main_agent_profile",
        lambda **_: events.append("agent_registry"),
    )

    service.update(
        {"DEFAULT_LANGUAGE": EnvironmentChange("replace", "en")},
        reload_after_write=True,
    )

    # 顺序即语义：必须先刷新 Settings，再把新预算注入主 Agent profile。
    assert events == ["settings", "agent_registry"]
    assert 'DEFAULT_LANGUAGE="en"' in env_file.read_text(encoding="utf-8")


def test_environment_service_validates_main_agent_max_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主 Agent 步数只能保存为正整数，并按标准 env 字段返回。"""

    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(
        "app.service.configuration.environment_configuration_service.system_cosir_dir",
        lambda: root,
    )
    env_file = root / ".env"
    _bind_env_file(monkeypatch, env_file)
    service = EnvironmentConfigurationService()

    service.update({"MAIN_AGENT_MAX_STEPS": EnvironmentChange("replace", "42")})
    assert 'MAIN_AGENT_MAX_STEPS="42"' in env_file.read_text(encoding="utf-8")
    field = next(item for item in service.read() if item["name"] == "MAIN_AGENT_MAX_STEPS")
    assert field["type"] == "integer"
    assert field["minimum"] == 1
    assert field["disk_value"] == "42"

    with pytest.raises(EnvironmentConfigurationError):
        service.update({"MAIN_AGENT_MAX_STEPS": EnvironmentChange("replace", "0")})
    with pytest.raises(EnvironmentConfigurationError):
        service.update({"MAIN_AGENT_MAX_STEPS": EnvironmentChange("replace", "not-a-number")})


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
                "model_config_id": None,
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
    env_file = tmp_path / ".env"
    env_file.write_text("DEFAULT_LANGUAGE=zh\n", encoding="utf-8")
    monkeypatch.delenv("DEFAULT_LANGUAGE", raising=False)
    monkeypatch.setattr("app.config.settings.paths.env_file", lambda: env_file)
    monkeypatch.setattr("app.config.settings.paths.SYSTEM_COSIR_DIR", tmp_path)
    monkeypatch.setattr(Settings, "_LOADED_ENV_VALUES", {})
    Settings._load_env_file()
    Settings._load_env_file()
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
    _bind_env_file(monkeypatch, root / ".env")
    service = EnvironmentConfigurationService()
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
