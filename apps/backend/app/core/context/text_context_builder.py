"""为第一个纯文本 Agent 工作流构建运行时消息。"""

from app.core.agents.agent_profile import AgentProfile, default_developer_agent
from app.models import RuntimeMessage, TurnRecord


class TextContextBuilder:
    """为一个任务构建纯文本模型上下文。"""

    def build_messages(
        self,
        agent_profile: AgentProfile,
        current_turn: TurnRecord,
        turn_history: list[TurnRecord] | None,
        message_store,
    ) -> list[RuntimeMessage]:
        """为当前轮构建与模型无关的运行时消息。

        完全基于 turn：system 提示词 + 前置轮的消息轨迹（来自 ``message_store``）+ 当前轮
        用户输入。不再依赖 task 执行态或构造假 turn 兜底。

        参数:
            agent_profile: 定义执行主体的 Agent 档案。
            current_turn: 当前运行轮次；其 ``input_text`` 作为本轮用户消息。
            turn_history: 当前任务的轮次历史（含当前轮），用于拼接前置轮轨迹。
            message_store: 消息轨迹存储（提供 ``load_messages(turn_id)``）。

        返回:
            交给模型适配器的、有序的运行时消息。

        异常:
            无。

        副作用:
            无。
        """

        profile = agent_profile or default_developer_agent()
        messages = [
            RuntimeMessage(
                role="system",
                content_text=_build_system_prompt(profile),
            )
        ]
        prior_turns = [
            turn
            for turn in (turn_history or [])
            if current_turn is None or turn.turn_id != current_turn.turn_id
        ]
        for turn in prior_turns:
            for message in message_store.load_turn_messages(turn.turn_id):
                messages.append(message)
        if current_turn is not None:
            messages.append(RuntimeMessage(role="user", content_text=current_turn.input_text))
        return messages


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
