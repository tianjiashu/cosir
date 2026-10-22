"""代码内置 Agent 与注册表装配契约测试。

专用 CHILD profile（code-developer / code-explorer / delegate_reviewer /
unit-test-engineer）原由 ``app/core/agents/defaults/*.json`` 随包分发，该目录已移除：
当前系统作用域由代码内置的 ``general-assistant``、``only_read_child_agent`` 与 ``main_agent`` 播种，用户自定义
CHILD 通过配置中心写入运行时的 ``system_agent_config_dir``。本模块因此以「process 作用域
能解析出哪些内置 profile」为断言对象，并用配置中心目录作为 JSON 装载路径。
"""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config.configuration import build_agent_registry
from app.core.agents.agent_profile import AgentProfileType
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.agent_profile_source import AgentProfileScopeSource
from app.core.agents.agent_runtime_config import AgentRuntimeConfig
from app.core.agents.define_agents import general_child_agent, main_agent
from app.core.agents.model_settings import ModelSettings
from app.core.context.system_prompt_builder import SystemPromptBuilder
from app.models.model_config_record import ModelConfigRecord


def _code_defined_profiles(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list:
    """构造只含代码内置 profile 的系统作用域目录。

    系统 JSON 目录被指向空目录：与启动装配一致（``build_agent_registry`` 只播种代码内置
    profile，运行时 JSON 由生命周期另行载入），避免用例受宿主机器上的用户配置影响。

    参数:
        monkeypatch: pytest 补丁夹具，用于把系统 Agent 目录指向临时空目录。
        tmp_path: 临时目录根。

    返回:
        系统作用域当前可见的 profile 列表。
    """

    monkeypatch.setattr(
        "app.core.agents.agent_profile_source.system_agent_config_dir",
        lambda: tmp_path / "agents",
    )
    registry = build_agent_registry()
    return registry.list(AgentProfileRegistry.SYSTEM_WORKSPACE)


def test_generic_child_profile_keeps_code_prompt(monkeypatch) -> None:
    """唯一代码内 CHILD 在 profile 装配时载入仓库提示词正文。"""

    # 该 profile 的 allowed_tools 含 delegate_task，工具能力目录层会读取进程内 Agent 目录；
    # 本用例只验证 Agent 预设层，故注入一个不含 CHILD 的空目录（该层因此不生成）。
    monkeypatch.setattr(
        "app.config.configuration.get_agent_registry",
        lambda: AgentProfileRegistry(AgentProfileScopeSource()),
    )
    profile = general_child_agent()
    prompt = SystemPromptBuilder.build(profile, str(Path(__file__).resolve()))

    assert profile.agent_type is AgentProfileType.CHILD
    assert profile.agent_id == "general-assistant"
    assert profile.system_prompt.strip()
    assert not hasattr(profile, "prompt_file_path")
    assert "You handle exactly one focused task delegated by the parent agent." in prompt


def test_builtin_child_profiles_are_code_defined_and_complete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """系统作用域播种代码内置 CHILD，且通用与只读 profile 均具有对应工具能力。"""

    profiles = _code_defined_profiles(monkeypatch, tmp_path)

    assert {profile.agent_id for profile in profiles} == {
        "general-assistant",
        "only_read_child_agent",
        "main_agent",
    }
    children = [profile for profile in profiles if profile.agent_type is AgentProfileType.CHILD]
    assert {profile.agent_id for profile in children} == {
        "general-assistant",
        "only_read_child_agent",
    }
    assert all(profile.system_prompt.strip() for profile in children)
    general = next(profile for profile in children if profile.agent_id == "general-assistant")
    assert {
        "write_file",
        "apply_patch",
        "delete_file",
        "move_file",
        "read_file",
    } <= set(general.allowed_tools)


def test_system_registry_contains_only_code_defined_children(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """进程目录只含代码内置 CHILD，不含已移除的 JSON 预设，也不含 workspace 配置。"""

    monkeypatch.setattr("app.config.configuration._TOOL_SYSTEM", None, raising=False)
    monkeypatch.setattr(
        "app.core.agents.agent_profile_source.system_agent_config_dir",
        lambda: tmp_path / "agents",
    )
    registry = build_agent_registry()

    builtins = {"general-assistant", "only_read_child_agent"}
    assert registry.child_agent_ids(AgentProfileRegistry.SYSTEM_WORKSPACE) == builtins
    assert registry.child_agent_ids(tmp_path / "workspace") == builtins


def test_main_profile_prompt_is_injected_and_child_catalog_follows_allowed_tools(
    monkeypatch,
) -> None:
    """主 Agent 提示词来自配置注入；子 Agent 目录只在 allowed_tools 含委派工具时进入工具层。"""

    profile = main_agent(system_prompt="主 Agent 执行协议")
    workspace_root = str(Path(__file__).resolve())

    def _fail() -> None:
        raise AssertionError("allowed_tools 不含委派工具时不应读取 Agent 目录")

    monkeypatch.setattr("app.config.configuration.get_agent_registry", _fail)
    profile_without_delegation = replace(profile, allowed_tools=["read_file"])
    prompt_without_tools = SystemPromptBuilder.build(profile_without_delegation, workspace_root)
    assert "<agent_layer>" in prompt_without_tools
    assert "主 Agent 执行协议" in prompt_without_tools
    assert "<tool_layer>" not in prompt_without_tools
    assert "general-assistant" not in prompt_without_tools

    registry = AgentProfileRegistry(AgentProfileScopeSource())
    registry.register(AgentProfileRegistry.SYSTEM_WORKSPACE, general_child_agent())
    monkeypatch.setattr(
        "app.config.configuration.get_agent_registry",
        lambda: registry,
    )
    # 主 Agent 同时声明 agent_team，工具层还会读取 Team 目录；本用例只关心 Agent 目录，
    # 注入空 Team 目录以免依赖宿主机器上的 Team 配置。
    monkeypatch.setattr(
        "app.agent_team.registry.get_agent_team_registry",
        lambda: SimpleNamespace(team_summary=lambda _workspace_root: ""),
    )
    prompt_with_delegation = SystemPromptBuilder.build(profile, workspace_root)

    assert profile.description is None
    assert not hasattr(profile, "prompt_file_path")
    assert "<tool_layer>" in prompt_with_delegation
    assert "general-assistant" in prompt_with_delegation


def test_main_profile_without_configured_prompt_skips_agent_layer() -> None:
    """未配置主 Agent prompt 时 profile 不带系统预设，Agent 预设层不生成。"""

    profile = main_agent()

    assert profile.system_prompt == ""
    assert SystemPromptBuilder._build_agent_layer(profile) == ""


def test_registry_projects_only_child_profiles() -> None:
    """registry 摘要和 ID 列表只暴露 CHILD，不包含 MAIN。"""

    registry = AgentProfileRegistry(AgentProfileScopeSource())
    generic = general_child_agent()
    main_profile = main_agent()
    registry.register(AgentProfileRegistry.SYSTEM_WORKSPACE, generic)
    registry.register(AgentProfileRegistry.SYSTEM_WORKSPACE, main_profile)

    assert registry.child_agent_ids(AgentProfileRegistry.SYSTEM_WORKSPACE) == {generic.agent_id}
    assert generic.agent_id in registry.child_agent_summary(AgentProfileRegistry.SYSTEM_WORKSPACE)
    assert main_profile.agent_id not in registry.child_agent_summary(
        AgentProfileRegistry.SYSTEM_WORKSPACE
    )


def test_builtin_children_leave_model_route_to_parent_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """内置子 Agent 不内置 provider/model，默认从父 Run 继承。"""

    profiles = _code_defined_profiles(monkeypatch, tmp_path)

    assert all(profile.model_config_id is None for profile in profiles)


def test_child_profile_model_settings_keep_custom_overrides() -> None:
    """运行时配置保留定义自有的模型偏好，不原地写共享定义。"""

    child = general_child_agent()
    custom = ModelSettings(temperature=0.2)
    child.model_settings = custom

    agent = AgentRuntimeConfig.resolve(
        child,
        ModelConfigRecord(
            id=7,
            config_name="run",
            base_url="https://run.test",
            api_key="key",
            model_name="run-model",
            context_window_k=64,
        ),
    )

    assert agent.model.temperature == 0.2
    assert agent.model.model_config_id == 7
    assert child.model_config_id is None
    assert child.model_settings is custom
