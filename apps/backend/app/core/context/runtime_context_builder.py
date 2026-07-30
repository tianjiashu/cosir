"""为第一个纯文本 Agent 工作流构建运行时消息。"""

from app.core.agents.agent_profile import AgentProfile, default_developer_agent
from app.core.context.system_prompt_builder import SystemPromptBuilder
from app.core.context.system_prompt_context import SystemPromptContext
from app.models import RuntimeMessage, TurnRecord
from app.tools.schemas import ToolExecutionContext


class RuntimeContextBuilder:
    """为一个任务构建纯文本模型上下文。"""

    def __init__(self, system_prompt_builder: SystemPromptBuilder | None = None) -> None:
        """初始化文本上下文构建器。

        参数:
            system_prompt_builder: 可选的系统提示词构建器；为空时使用默认实现。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """

        self._system_prompt_builder = system_prompt_builder or SystemPromptBuilder()

    def build_messages(
            self,
            agent_profile: AgentProfile,
            current_turn: TurnRecord,
            turn_history: list[TurnRecord] | None,
            message_store,
            execution_context: ToolExecutionContext | None = None,
    ) -> list[RuntimeMessage]:
        """为当前轮构建与模型无关的运行时消息。

        完全基于 turn：system 提示词 + 前置轮的消息轨迹（来自 ``message_store``）+ 当前轮
        用户输入。不再依赖 task 执行态或构造假 turn 兜底。

        参数:
            agent_profile: 定义执行主体的 Agent 档案。
            current_turn: 当前运行轮次；其 ``input_text`` 作为本轮用户消息。
            turn_history: 当前任务的轮次历史（含当前轮），用于拼接前置轮轨迹。
            message_store: 消息轨迹存储（提供 ``load_turn_messages(turn_id)``）。
            execution_context: 当前工具执行边界；用于向系统提示词注入 workspace root。

        返回:
            交给模型适配器的、有序的运行时消息。

        异常:
            无。

        副作用:
            无。
        """

        profile = agent_profile or default_developer_agent()
        workspace_root = (
            str(execution_context.workspace_root) if execution_context is not None else None
        )
        prompt_context = SystemPromptContext.build(workspace_root=workspace_root)
        messages = [
            RuntimeMessage(
                role="system",
                content_text=self._system_prompt_builder.build(profile, prompt_context),
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
