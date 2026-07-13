"""供运行时任务使用的 Agent profile 值对象。"""

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class AgentProfile:
    """描述某个任务的 Agent 执行主体。

    参数:
        agent_id: 持久化在任务和事件上的稳定 Agent 标识。
        role: 人类可读的 Agent 角色。
        goal: 注入到模型上下文中的运行目标。
        allowed_tools: 该 Agent 允许使用的工具名或权限名。
        context_policy: 该 Agent 的上下文处理策略名称。

    返回:
        不可变的 Agent profile 值对象。

    异常:
        无。

    副作用:
        无。
    """

    agent_id: str
    role: str
    goal: str
    allowed_tools: Tuple[str, ...]
    context_policy: str

    def allows_tool(self, tool_name: str, permission: str) -> bool:
        """返回该 Agent profile 是否允许某个工具。

        参数:
            tool_name: 已注册的稳定工具名。
            permission: 该工具所需的权限级别。

        返回:
            当 profile 允许该工具名或该权限名时返回 True。

        异常:
            无。

        副作用:
            无。
        """

        allowed = set(self.allowed_tools)
        return tool_name in allowed or permission in allowed

    def to_dict(self) -> dict:
        """将 Agent profile 转换为可 JSON 序列化的字典。

        参数:
            无。

        返回:
            包含稳定 Agent profile 字段的字典。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "goal": self.goal,
            "allowed_tools": list(self.allowed_tools),
            "context_policy": self.context_policy,
        }


def default_developer_agent() -> AgentProfile:
    """构建第一版默认的开发者 Agent profile。

    参数:
        无。

    返回:
        用于内置单 Agent 的 AgentProfile。

    异常:
        无。

    副作用:
        无。
    """

    return AgentProfile(
        agent_id="developer",
        role="developer",
        goal=(
            "完成本地 coding-agent 任务；优先保持代码清晰、可诊断、可扩展，"
            "第一版只处理纯文本输入。"
        ),
        allowed_tools=("safe_read",),
        context_policy="text_only_v1",
    )
