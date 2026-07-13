"""为第一个纯文本 Agent 工作流构建运行时消息。"""

from typing import List

from app.agents.profile import AgentProfile, default_developer_agent
from app.models.base import RuntimeMessage
from app.storage.records import TaskRecord


class TextContextBuilder:
    """为一个任务构建纯文本模型上下文。"""

    def build_messages(
        self,
        task: TaskRecord,
        agent_profile: AgentProfile = None,
    ) -> List[RuntimeMessage]:
        """为一个任务构建与模型无关的运行时消息。

        参数:
            task: 包含用户输入文本的任务记录。
            agent_profile: 定义执行主体的 Agent 档案。

        返回:
            交给模型适配器的、有序的运行时消息。

        异常:
            无。

        副作用:
            无。
        """

        profile = agent_profile or default_developer_agent()
        return [
            RuntimeMessage(
                role="system",
                content_text=_build_system_prompt(profile),
            ),
            RuntimeMessage(role="user", content_text=task.input_text),
        ]


def _build_system_prompt(agent_profile: AgentProfile) -> str:
    """从 Agent 档案构建一个纯文本系统提示词。

    参数:
        agent_profile: 需要注入模型上下文的 Agent 档案。

    返回:
        描述 Agent 角色、目标与上下文策略的系统提示词文本。

    异常:
        无。

    副作用:
        无。
    """

    return (
        f"你是一个本地 coding-agent，当前 Agent ID 是 {agent_profile.agent_id}，"
        f"角色是 {agent_profile.role}。"
        f"目标：{agent_profile.goal}"
        f"允许工具：{', '.join(agent_profile.allowed_tools) or 'none'}。"
        f"上下文策略：{agent_profile.context_policy}。"
        "第一版只处理纯文本输入，并以清晰、可执行的方式回复用户。"
    )
