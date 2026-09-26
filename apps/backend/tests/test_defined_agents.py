"""代码内 Agent 与 JSON 配置型子 Agent 的装配契约测试。"""

from dataclasses import dataclass, replace
from pathlib import Path

from app.config.configuration import build_agent_registry
from app.core.agents.agent_profile import AgentProfileType
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.define_agents import general_child_agent, main_agent
from app.core.agents.model_settings import ModelSettings
from app.core.context.system_prompt_builder import SystemPromptBuilder

_DEFAULTS_DIR = Path(__file__).resolve().parents[1] / "app" / "core" / "agents" / "defaults"


def _default_profiles():
    """从随应用分发的默认 JSON 构造测试用系统作用域目录。"""

    registry = AgentProfileRegistry()
    registry.load_agent_profiles(AgentProfileRegistry.SYSTEM_WORKSPACE, _DEFAULTS_DIR)
    return registry.list(AgentProfileRegistry.SYSTEM_WORKSPACE)


@dataclass(frozen=True)
class _RunRoute:
    """Conversation Run 的最小替身：只承载 `derive_for_run` 读取的路由字段。"""

    provider_id: int | None = None
    model_name: str | None = None


def test_generic_child_profile_keeps_code_prompt(monkeypatch) -> None:
    """唯一代码内 CHILD 在 profile 装配时载入仓库提示词正文。"""

    # 该 profile 的 allowed_tools 含 delegate_task，工具能力目录层会读取进程内 Agent 目录；
    # 本用例只验证 Agent 预设层，故注入一个不含 CHILD 的空目录（该层因此不生成）。
    monkeypatch.setattr(
        "app.config.configuration.get_agent_registry",
        AgentProfileRegistry,
    )
    profile = general_child_agent()
    prompt = SystemPromptBuilder.build(profile, str(Path(__file__).resolve()))

    assert profile.agent_type is AgentProfileType.CHILD
    assert profile.agent_id == "general-assistant"
    assert profile.system_prompt.strip()
    assert not hasattr(profile, "prompt_file_path")
    assert "general-purpose child agent" in prompt


def test_default_child_profiles_load_from_json() -> None:
    """专用 CHILD profile 与其系统提示词均从随应用分发的 JSON 装载。"""

    profiles = _default_profiles()

    assert {profile.agent_id for profile in profiles} == {
        "delegate_reviewer",
        "code-explorer",
        "unit-test-engineer",
        "code-developer",
    }
    assert all(profile.agent_type is AgentProfileType.CHILD for profile in profiles)
    assert all(profile.system_prompt and profile.system_prompt.strip() for profile in profiles)
    coder = next(profile for profile in profiles if profile.agent_id == "code-developer")
    assert {"apply_patch", "delete_file", "move_file"} <= set(coder.allowed_tools)


def test_system_registry_contains_generic_and_loaded_children(monkeypatch) -> None:
    """进程目录包含代码内通用 CHILD 与显式载入的系统 JSON，不包含 workspace 配置。"""

    monkeypatch.setattr("app.config.configuration._TOOL_SYSTEM", None, raising=False)
    registry = build_agent_registry()
    registry.load_agent_profiles(AgentProfileRegistry.SYSTEM_WORKSPACE, _DEFAULTS_DIR)

    assert registry.child_agent_ids(AgentProfileRegistry.SYSTEM_WORKSPACE) == {
        "general-assistant",
        "delegate_reviewer",
        "code-explorer",
        "unit-test-engineer",
        "code-developer",
    }


def test_main_profile_uses_own_prompt_and_child_catalog_follows_allowed_tools(monkeypatch) -> None:
    """主 Agent 使用独立提示词；子 Agent 目录只在 allowed_tools 含委派工具时进入工具层。"""

    profile = main_agent()
    workspace_root = str(Path(__file__).resolve())

    def _fail() -> None:
        raise AssertionError("allowed_tools 不含委派工具时不应读取 Agent 目录")

    monkeypatch.setattr("app.config.configuration.get_agent_registry", _fail)
    profile_without_delegation = replace(profile, allowed_tools=["read_file"])
    prompt_without_tools = SystemPromptBuilder.build(profile_without_delegation, workspace_root)
    assert "<tool_layer>" not in prompt_without_tools
    assert "general-assistant" not in prompt_without_tools

    registry = AgentProfileRegistry()
    registry.register(AgentProfileRegistry.SYSTEM_WORKSPACE, general_child_agent())
    monkeypatch.setattr(
        "app.config.configuration.get_agent_registry",
        lambda: registry,
    )
    prompt_with_delegation = SystemPromptBuilder.build(profile, workspace_root)

    assert profile.description is None
    assert profile.system_prompt.strip()
    assert not hasattr(profile, "prompt_file_path")
    assert "主 Agent" in prompt_with_delegation
    assert "用户明确限定修改范围时，范围是硬约束" in prompt_with_delegation
    assert "<tool_layer>" in prompt_with_delegation
    assert "general-assistant" in prompt_with_delegation


def test_registry_projects_only_child_profiles() -> None:
    """registry 摘要和 ID 列表只暴露 CHILD，不包含 MAIN。"""

    registry = AgentProfileRegistry()
    generic = general_child_agent()
    main_profile = main_agent()
    registry.register(AgentProfileRegistry.SYSTEM_WORKSPACE, generic)
    registry.register(AgentProfileRegistry.SYSTEM_WORKSPACE, main_profile)

    assert registry.child_agent_ids(AgentProfileRegistry.SYSTEM_WORKSPACE) == {generic.agent_id}
    assert generic.agent_id in registry.child_agent_summary(AgentProfileRegistry.SYSTEM_WORKSPACE)
    assert main_profile.agent_id not in registry.child_agent_summary(
        AgentProfileRegistry.SYSTEM_WORKSPACE
    )


def test_json_children_leave_model_route_to_parent_run() -> None:
    """默认配置型子 Agent 不内置 provider/model，默认从父 Run 继承。"""

    profiles = _default_profiles()

    assert all(profile.provider_id is None for profile in profiles)
    assert all(profile.model_name is None for profile in profiles)
    assert all(profile.model_settings == ModelSettings() for profile in profiles)


def test_child_profile_model_settings_keep_custom_overrides() -> None:
    """per-run 派生保留 profile 自有模型参数覆盖。"""

    child = _default_profiles()[0]
    custom = ModelSettings(temperature=0.2, thinking=True)
    child.model_settings = custom

    derived = child.derive_for_run(
        run=_RunRoute(provider_id=7, model_name="glm-4.6"),  # type: ignore[arg-type]
    )

    assert derived.model_settings is custom
    assert derived.model_settings.temperature == 0.2
    assert derived.model_settings.thinking is True
    assert (derived.provider_id, derived.model_name) == (7, "glm-4.6")
    assert (child.provider_id, child.model_name, child.run) == (None, None, None)
