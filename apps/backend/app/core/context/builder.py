"""为第一个纯文本 Agent 工作流构建运行时消息。"""

from app.core.agents.profile import AgentProfile, default_developer_agent
from app.models import RuntimeMessage
from app.models import TaskRecord
from app.models import TurnRecord


class TextContextBuilder:
    """为一个任务构建纯文本模型上下文。"""

    def build_messages(
        self,
        task: TaskRecord,
        agent_profile: AgentProfile = None,
        turn: TurnRecord = None,
        turn_history: list[TurnRecord] | None = None,
    ) -> list[RuntimeMessage]:
        """为一个任务构建与模型无关的运行时消息。

        参数:
            task: 包含用户输入文本的任务记录。
            agent_profile: 定义执行主体的 Agent 档案。
            turn: 可选的当前运行轮次；提供时使用该轮次输入作为用户消息。
            turn_history: 可选的任务轮次历史；提供时按轮次顺序构建用户消息。

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
        turns = _select_turns(task, turn, turn_history)
        messages.extend(RuntimeMessage(role="user", content_text=item.input_text) for item in turns)
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


def _select_turns(
    task: TaskRecord,
    turn: TurnRecord | None,
    turn_history: list[TurnRecord] | None,
) -> list[TurnRecord]:
    """选择应进入模型上下文的轮次输入。

    参数:
        task: 当前任务记录，用于兼容没有 turn 记录的旧任务输入。
        turn: 当前运行轮次。
        turn_history: 当前任务的轮次历史。

    返回:
        按模型上下文顺序排列的轮次列表。

    异常:
        无。

    副作用:
        无。
    """

    if turn_history:
        if turn is None:
            return turn_history
        selected = []
        for item in turn_history:
            selected.append(item)
            if item.turn_id == turn.turn_id:
                return selected
        return [*turn_history, turn]
    if turn is not None:
        return [turn]
    fallback_turn = TurnRecord(
        turn_id=task.latest_turn_id or task.task_id,
        task_id=task.task_id,
        input_text=task.input_text,
        status=task.status,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )
    return [fallback_turn]
