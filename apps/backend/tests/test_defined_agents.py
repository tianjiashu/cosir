"""内置 Agent profile 与系统提示词契约测试。"""

from pathlib import Path

from app.core.agents.agent_profile import AgentProfileType
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.define_agents import (
    coder_agent,
    explorer_agent,
    main_agent,
    reviewer_agent,
)
from app.core.agents.define_agents import (
    test_agent as build_test_agent,
)
from app.core.agents.model_settings import ModelSettings
from app.core.context.system_prompt_builder import SystemPromptBuilder


class _ToolRegistryStub:
    """为 profile 工厂提供最小工具注册表替身。"""

    def get_all_tool_names(self) -> list[str]:
        """返回测试所需的最小工具名集合。"""

        return [
            "read_file",
            "write_file",
            "patch_write",
            "apply_patch",
            "delete_file",
            "move_file",
            "search_content",
            "find_files",
            "list_directory",
            "execute_terminal",
            "web_search",
            "web_extract",
            "delegate_task",
        ]


def test_builtin_child_profiles_have_prompt_files(monkeypatch) -> None:
    """四个内置子 Agent 应绑定存在的专属系统提示词文件。"""

    monkeypatch.setattr(
        "app.core.agents.define_agents.get_tool_registry",
        lambda: _ToolRegistryStub(),
    )

    profiles = [reviewer_agent(), explorer_agent(), build_test_agent(), coder_agent()]

    assert all(profile.prompt_file_path is not None for profile in profiles)
    assert all(isinstance(profile.prompt_file_path, Path) for profile in profiles)
    assert all(
        profile.prompt_file_path.is_file() for profile in profiles if profile.prompt_file_path
    )
    assert all(profile.agent_type is AgentProfileType.CHILD for profile in profiles)
    coder = next(profile for profile in profiles if profile.agent_id == "code-developer")
    assert {"apply_patch", "delete_file", "move_file"} <= set(coder.allowed_tools or [])

    expected_prompt_markers = [
        ("delegate_reviewer", "不得写入、删除或修改任何文件"),
        ("code-explorer", "不要为了普通本地代码问题联网"),
        ("unit-test-engineer", "不得修改生产代码"),
        ("code-developer", "不做无关重构"),
    ]
    for profile, (agent_id, marker) in zip(profiles, expected_prompt_markers, strict=True):
        prompt = SystemPromptBuilder.build(profile, str(Path(__file__).resolve()))
        assert "<agent_layer>" in prompt
        assert agent_id in prompt
        assert marker in prompt


def test_main_profile_uses_own_prompt_without_child_catalog(monkeypatch) -> None:
    """主 Agent 使用独立提示词，子 Agent 清单只由委派工具提供。"""

    monkeypatch.setattr(
        "app.core.agents.define_agents.get_tool_registry",
        lambda: _ToolRegistryStub(),
    )

    profile = main_agent()
    prompt = SystemPromptBuilder.build(profile, str(Path(__file__).resolve()))

    assert profile.description is None
    assert profile.prompt_file_path is not None
    assert profile.prompt_file_path.name == "main_agent.md"
    assert "主 Agent" in prompt
    assert "用户明确限定修改范围时，范围是硬约束" in prompt
    for child_id in (
        "delegate_reviewer",
        "code-explorer",
        "unit-test-engineer",
        "code-developer",
    ):
        assert child_id not in prompt


def test_registry_exposes_non_main_profiles_as_delegation_targets(monkeypatch) -> None:
    """注册表应向主 Agent 投影子 Agent，而不是把主 Agent 当成子 Agent。"""

    monkeypatch.setattr(
        "app.core.agents.define_agents.get_tool_registry",
        lambda: _ToolRegistryStub(),
    )

    registry = AgentProfileRegistry()
    children = [reviewer_agent(), explorer_agent(), build_test_agent(), coder_agent()]
    main_profile = main_agent()
    for profile in [*children, main_profile]:
        registry.register(profile)

    child_ids = {profile.agent_id for profile in children}
    assert registry.child_agent_ids() == child_ids
    summary = registry.child_agent_summary()
    assert all(agent_id in summary for agent_id in child_ids)
    # 「主 Agent 不得进入子 Agent 清单」这一不变量的断言必须与渲染格式无关：旧格式
    # （``agent_id: X ==> role: Y ==> ...``）与新格式（``agent_id: X | description: ...``）
    # 下都要能抓到越界投影，避免格式变更把断言变成永远成立的空转。
    assert main_profile.agent_id not in summary
    # 正向兜底：条目数必须等于 CHILD 数（多渲染一行即说明越界投影）。每个条目单行由
    # ``child_agent_summary`` 的渲染契约保证（描述与工具清单都不得含换行）。
    assert len(summary.splitlines()) - 1 == len(child_ids)


def test_builtin_child_profiles_leave_model_route_to_parent_run(monkeypatch) -> None:
    """内置 child profile 不应写死 provider/model，默认由父 Run 决定。"""

    monkeypatch.setattr(
        "app.core.agents.define_agents.get_tool_registry",
        lambda: _ToolRegistryStub(),
    )

    profiles = [reviewer_agent(), explorer_agent(), build_test_agent(), coder_agent()]

    assert all(profile.provider_id is None for profile in profiles)
    assert all(profile.model_name is None for profile in profiles)
    assert all(profile.model_settings == ModelSettings() for profile in profiles)


def test_child_profile_model_settings_keep_custom_overrides(monkeypatch) -> None:
    """child profile 只补齐未配置参数，显式配置仍覆盖父 Agent 默认值。"""

    monkeypatch.setattr(
        "app.core.agents.define_agents.get_tool_registry",
        lambda: _ToolRegistryStub(),
    )
    parent = main_agent()
    child = reviewer_agent()
    child.model_settings = ModelSettings(temperature=0.2)

    derived = child.derive_for_run(
        run=object(),  # type: ignore[arg-type]
        model_defaults=parent,
    )

    assert derived.model_settings.temperature == 0.2
    assert derived.model_settings.thinking is True
    assert derived.model_settings.stream is True
    assert derived.model_settings.reasoning_effort == "high"
