"""system prompt builder tests."""

from dataclasses import replace

from app.core.agents.agent_profile import default_developer_agent
from app.core.context.system_prompt_builder import SystemPromptBuilder


def _rich_profile():
    """构造带 capabilities 与 constraints 的 developer profile。

    参数:
        无。

    返回:
        包含非空 capabilities / constraints 的 AgentProfile。

    异常:
        无。

    副作用:
        无。
    """
    return replace(
        default_developer_agent(),
        description="协助用户完成软件工程项目开发任务",
        capabilities=["代码开发", "缺陷修复"],
        constraints=["不泄露密钥", "不递归委派"],
    )


def test_agent_identity_injects_description_and_capabilities_constraints():
    """验证身份 section 注入 description、capabilities 与 constraints 行。

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
    assert "能力: 代码开发, 缺陷修复" in text
    assert "约束: 不泄露密钥, 不递归委派" in text
    assert "goal" not in text


def test_agent_identity_omits_empty_capabilities_constraints_lines():
    """验证 capabilities / constraints 为空时不产生空噪音行。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当身份文本出现空能力或约束行时由 pytest 抛出。

    副作用:
        无。
    """
    profile = default_developer_agent()
    assert profile.capabilities == []
    assert profile.constraints == []
    text = SystemPromptBuilder._agent_identity(profile, "/workspace", "zh")

    assert "能力:" not in text
    assert "约束:" not in text
    assert "goal" not in text


def test_build_uses_rich_identity_section():
    """验证 build 入口产出的系统提示词包含身份 section 与能力约束。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 当完整提示词缺失能力或约束行时由 pytest 抛出。

    副作用:
        无。
    """
    profile = _rich_profile()
    prompt = SystemPromptBuilder.build(profile, "/workspace")

    assert "<agent_identity>" in prompt
    assert "能力: 代码开发, 缺陷修复" in prompt
    assert "约束: 不泄露密钥, 不递归委派" in prompt
