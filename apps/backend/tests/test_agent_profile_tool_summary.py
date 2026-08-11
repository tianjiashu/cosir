"""子 Agent 能力摘要投影测试。"""

from app.config.configuration import build_agent_registry, get_delegate_agent_summary
from app.core.agents.agent_profile import (
    DEFAULT_AGENT_ID,
    AgentProfile,
    default_developer_agent,
)
from app.core.agents.agent_profile_tool_summary import project_child_agent_summary
from app.core.agents.delegate_agent_profiles import (
    delegate_analyst_agent,
    delegate_coder_agent,
    delegate_reviewer_agent,
)


def _make_registry() -> object:
    """构造含 developer + 三个 delegate 子 Agent 的注册表，模拟默认目录。

    参数:
        无。

    返回:
        已注册的 ``AgentProfileRegistry`` 实例。

    异常:
        无。

    副作用:
        无。
    """

    from app.core.agents.agent_profile_registry import AgentProfileRegistry

    registry = AgentProfileRegistry()
    registry.register(default_developer_agent())
    registry.register(delegate_reviewer_agent())
    registry.register(delegate_analyst_agent())
    registry.register(delegate_coder_agent())
    return registry


def test_project_child_agent_summary_only_includes_delegate_prefix():
    """摘要只含 delegate_ 前缀子 Agent，不含 developer / developer_pro。"""

    registry = _make_registry()
    summary = project_child_agent_summary(registry)

    assert "delegate_reviewer" in summary
    assert "delegate_analyst" in summary
    assert "delegate_coder" in summary
    assert DEFAULT_AGENT_ID not in summary
    assert "developer_pro" not in summary


def test_project_child_agent_summary_contains_role_and_description():
    """摘要包含三个子 Agent 的 role 与 description。"""

    registry = _make_registry()
    summary = project_child_agent_summary(registry)

    for profile in (
        delegate_reviewer_agent(),
        delegate_analyst_agent(),
        delegate_coder_agent(),
    ):
        assert profile.role in summary
        assert profile.description in summary


def test_project_child_agent_summary_excludes_workflow():
    """摘要不得泄漏运行时字段（workflow）。"""

    registry = _make_registry()
    summary = project_child_agent_summary(registry)

    assert "workflow" not in summary


def test_project_child_agent_summary_includes_tool_capability_summary():
    """摘要应包含由 allowed_tools 渲染的工具能力摘要（tools: 片段）。"""

    registry = _make_registry()
    summary = project_child_agent_summary(registry)

    assert "tools:" in summary


def test_project_child_agent_summary_empty_when_no_children():
    """注册表无任何 delegate_ 子 Agent 时返回空串。"""

    from app.core.agents.agent_profile_registry import AgentProfileRegistry

    registry = AgentProfileRegistry()
    registry.register(
        AgentProfile(
            agent_id=DEFAULT_AGENT_ID,
            role="coding-agent-flush",
            description="locomotive",
            allowed_tools=[],
            context_policy="text_only_v1",
        )
    )
    assert project_child_agent_summary(registry) == ""


def test_build_agent_registry_injects_summary_singleton():
    """build_agent_registry 注入摘要单例，且只含 delegate_ 子 Agent。"""

    build_agent_registry()
    summary = get_delegate_agent_summary()

    assert summary.startswith("Available child agents:")
    assert "delegate_reviewer" in summary
    assert "delegate_analyst" in summary
    assert "delegate_coder" in summary
    assert DEFAULT_AGENT_ID not in summary
    assert "workflow" not in summary
    assert "tools:" in summary
