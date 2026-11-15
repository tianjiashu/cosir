"""测试用：把内置子 Agent 的出厂配置转成 profile。

生产里内置子 Agent 来自系统 ``.cosir/agents`` 下的文件；只想拿到 profile 的用例不必先落盘，
这里按装载时同一个校验入口（``parse_agent_profile_document``）直接转换。
"""

from pathlib import Path

from app.core.agents.agent_profile import AgentProfile, parse_agent_profile_document
from app.service.configuration.agents.builtin_agents import BUILTIN_CHILD_AGENTS


def builtin_child_profile(agent_id: str) -> AgentProfile:
    configuration = next(agent for agent in BUILTIN_CHILD_AGENTS if agent.agent_id == agent_id)
    return parse_agent_profile_document(configuration.to_json_document(), Path(f"{agent_id}.json"))
