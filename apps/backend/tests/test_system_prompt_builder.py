"""system prompt builder tests."""

from app.core.agents.define_agents import default_developer_agent
from app.core.context.system_prompt_builder import SystemPromptBuilder


def _rich_profile():
    """构造带丰富 description 的 developer profile（能力/约束已收敛进 description）。

    参数:
        无。

    返回:
        包含非空 description 的 AgentProfile。

    异常:
        无。

    副作用:
        无。
    """
    return default_developer_agent()


def test_agent_identity_injects_description():
    """验证身份 section 注入 description（能力/约束已收敛进 description 文本）。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当身份文本缺失期望内容时由 pytest 抛出。

    副作用:
        无。
    """
    profile = _rich_profile()
    text = SystemPromptBuilder._agent_identity(profile, "/workspace", "zh")

    assert profile.description in text
    assert "goal" not in text


def test_agent_identity_omits_split_capability_constraint_lines():
    """验证 identity 不再产出独立的「能力:」「约束:」行（已收敛至 description）。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当身份文本出现拆分的能力或约束行时由 pytest 抛出。

    副作用:
        无。
    """
    profile = default_developer_agent()
    text = SystemPromptBuilder._agent_identity(profile, "/workspace", "zh")

    assert "能力:" not in text
    assert "约束:" not in text
    assert "goal" not in text


def test_build_uses_identity_section_with_description():
    """验证 build 入口产出的系统提示词包含身份 section 与 description。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当完整提示词缺失身份 section 时由 pytest 抛出。

    副作用:
        无。
    """
    profile = _rich_profile()
    prompt = SystemPromptBuilder.build(profile, "/workspace")

    assert "<agent_identity>" in prompt
    assert profile.description in prompt
