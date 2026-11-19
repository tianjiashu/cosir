"""主 Agent 与内置子 Agent 的装配契约测试。

系统作用域由代码维护的 ``main_agent`` 与内置子 Agent（``general-purpose``、``Explore``）
组成：内置子 Agent 的事实源是系统 ``.cosir/agents`` 下的配置文件，启动装配在文件缺失时写入出厂值。
本模块以「系统作用域能解析出哪些 profile、文件与内存怎样对应」为断言对象。
"""

from dataclasses import replace
from pathlib import Path

import pytest

from app.config.configuration import build_agent_registry
from app.core.agents.agent_profile import AgentProfileType
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.agent_runtime_config import AgentRuntimeConfig
from app.core.agents.define_agents import main_agent
from app.core.agents.model_settings import ModelSettings
from app.core.context.prompt_sources.registry import AGENT_PRESET
from app.core.context.prompt_sources.source import SourceContext
from app.core.context.system_prompt_builder import SystemPromptBuilder
from tests.builtin_agent_fixtures import builtin_child_profile
from tests.model_config_fixtures import model_config_record
from tests.static_agent_source import registry_with


def _code_defined_profiles(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list:
    """按启动装配构造系统作用域：从空目录开始，内置子 Agent 文件由装配补齐。

    系统 JSON 目录被指向临时空目录，避免用例受宿主机器上的用户配置影响。

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


def test_general_child_profile_carries_its_factory_prompt(monkeypatch) -> None:
    """通用子 Agent 的出厂配置带着自己的提示词正文，进入系统提示词的 Agent 预设层。"""

    # 该 profile 的 allowed_tools 含 delegate_task，工具能力目录层会读取进程内 Agent 目录；
    # 本用例只验证 Agent 预设层，故注入一个不含 CHILD 的空目录（该层因此不生成）。
    monkeypatch.setattr(
        "app.config.configuration.get_agent_registry",
        lambda: registry_with()[0],
    )
    profile = builtin_child_profile("general-purpose")
    prompt = SystemPromptBuilder.build(
        profile, str(Path(__file__).resolve()), profile.allowed_tools
    ).text

    assert profile.agent_type is AgentProfileType.CHILD
    assert profile.agent_id == "general-purpose"
    assert profile.system_prompt.strip()
    assert not hasattr(profile, "prompt_file_path")
    assert "You handle exactly one focused task delegated by the parent agent." in prompt


def test_builtin_child_profiles_are_seeded_and_complete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """系统作用域含主 Agent 与三个内置子 Agent，且通用与只读 profile 均具有对应工具能力。"""

    profiles = _code_defined_profiles(monkeypatch, tmp_path)

    assert {profile.agent_id for profile in profiles} == {
        "general-purpose",
        "Explore",
        "Plan",
        "main_agent",
    }
    children = [profile for profile in profiles if profile.agent_type is AgentProfileType.CHILD]
    assert {profile.agent_id for profile in children} == {
        "general-purpose",
        "Explore",
        "Plan",
    }
    assert all(profile.system_prompt.strip() for profile in children)
    general = next(profile for profile in children if profile.agent_id == "general-purpose")
    assert {
        "write_file",
        "apply_patch",
        "read_file",
    } <= set(general.allowed_tools)
    assert not {"delete_file", "move_file"} & set(general.allowed_tools)


def test_system_registry_contains_only_builtin_children(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """空目录起步时只有三个内置子 Agent，workspace 作用域也只继承它们。"""

    monkeypatch.setattr("app.config.configuration._TOOL_REGISTRY", None)
    monkeypatch.setattr("app.config.configuration._TOOL_EXECUTOR", None)
    monkeypatch.setattr(
        "app.core.agents.agent_profile_source.system_agent_config_dir",
        lambda: tmp_path / "agents",
    )
    registry = build_agent_registry()

    builtins = {"general-purpose", "Explore", "Plan"}
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
    prompt_without_tools = SystemPromptBuilder.build(
        profile_without_delegation, workspace_root, profile_without_delegation.allowed_tools
    ).text
    assert "<agent_layer>" in prompt_without_tools
    assert "主 Agent 执行协议" in prompt_without_tools
    # 没有委派工具时不投影子 Agent 目录；文件工具的公共声明照常注入。
    assert "Available child agents" not in prompt_without_tools
    assert "read_file to read" in prompt_without_tools
    assert "general-purpose" not in prompt_without_tools

    registry, _ = registry_with([builtin_child_profile("general-purpose")])
    monkeypatch.setattr(
        "app.config.configuration.get_agent_registry",
        lambda: registry,
    )
    prompt_with_delegation = SystemPromptBuilder.build(
        profile, workspace_root, profile.allowed_tools
    ).text

    assert profile.description is None
    assert not hasattr(profile, "prompt_file_path")
    assert "<tool_layer>" in prompt_with_delegation
    assert "general-purpose" in prompt_with_delegation


def test_main_profile_without_configured_prompt_skips_agent_layer() -> None:
    """未配置主 Agent prompt 时 profile 不带系统预设，Agent 预设层不生成。"""

    profile = main_agent()

    assert profile.system_prompt == ""
    assert AGENT_PRESET.project(SourceContext.locate("ws", frozenset(), profile)) == {}


def test_registry_projects_only_child_profiles() -> None:
    """registry 摘要和 ID 列表只暴露 CHILD，不包含 MAIN。"""

    generic = builtin_child_profile("general-purpose")
    main_profile = main_agent()
    registry, _ = registry_with([generic, main_profile])

    assert registry.child_agent_ids(AgentProfileRegistry.SYSTEM_WORKSPACE) == {generic.agent_id}
    child_ids = {
        profile.agent_id
        for profile in registry.list(AgentProfileRegistry.SYSTEM_WORKSPACE)
        if profile.agent_type is AgentProfileType.CHILD
    }
    assert child_ids == {generic.agent_id}


def test_builtin_children_leave_model_route_to_parent_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """内置子 Agent 不内置 provider/model，默认从父 Run 继承。"""

    profiles = _code_defined_profiles(monkeypatch, tmp_path)

    assert all(profile.model_config_id is None for profile in profiles)


def test_child_profile_model_settings_keep_custom_overrides() -> None:
    """运行时配置保留定义自有的模型偏好，不原地写共享定义。"""

    child = builtin_child_profile("general-purpose")
    custom = ModelSettings(temperature=0.2)
    child.model_settings = custom

    agent = AgentRuntimeConfig.resolve(
        child,
        model_config_record(
            name="run", base_url="https://run.test", api_key="key",
            model_name="run-model",
            context_window_k=64,
        ),
    )

    assert agent.model.temperature == 0.2
    assert agent.model.model_config_id == "test/run-model"
    assert child.model_config_id is None
    assert child.model_settings is custom
