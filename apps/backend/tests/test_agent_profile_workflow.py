"""AgentProfile.workflow_id 透传测试。

锁定 L6 修复：workflow_id 由 AgentWorkflow Protocol 声明，消费方直接访问而非
getattr 静默回退。本测试验证默认 ReactLikeWorkflow 的 workflow_id 能正确透传。
"""

from app.core.agents.agent_profile import AgentProfile


def _make_profile() -> AgentProfile:
    """构造一个最小可用的 AgentProfile（默认 ReAct-like workflow）。"""
    return AgentProfile(
        agent_id="test-agent",
        role="developer",
        description="test",
        allowed_tools=[],
    )


def test_default_workflow_id_is_react_like_v1():
    """默认 workflow 的 workflow_id 透传为 react_like_v1（而非回退 custom）。"""
    profile = _make_profile()
    assert profile.to_dict()["workflow"] == "react_like_v1"


def test_workflow_id_not_fallback_to_custom():
    """to_dict 不再产生隐式回退值 custom。"""
    profile = _make_profile()
    assert profile.to_dict()["workflow"] != "custom"
