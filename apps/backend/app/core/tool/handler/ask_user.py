"""ask_user 工具：模型被一个真正属于用户的决定卡住时，向用户提问（对标 Claude Code
``AskUserQuestion``，设计见 ``docs/plan/ask-user-design.md``）。

``reply`` 类 HITL：调用只登记待答事项——返回等待占位、Run 挂起；用户在提问卡片里作答后，回答经
通用决定入口（``/tool-calls/{id}/decision``）以 ``AskUserAnswers`` 载荷送达，渲染成一条
``<user_decision>`` 交给模型，同一个 Run 接着干。本工具没有自己的业务事实：等待与否看占位行，
回答在占位的展示数据里。

提问只在发起它的回合内有效（``decision_lifetime="turn"``）：用户不理卡片直接发消息、或模型又问
一次，旧提问作废（``turn_decision_expiry``）。只给侧栏会话（``user_decision`` 工具天然需要用户界面），
子 Agent 需要用户决定时在结论里交代，由上级去问。
"""

from typing import Any, ClassVar

from app.core.tool.handler.question.ask_user_answers import AskUserAnswers
from app.core.tool.handler.question.ask_user_args import AskUserArgs
from app.core.tool.results.tool_awaiting_decision import tool_awaiting_decision
from app.core.tool.schemas import (
    TOOL_ASK_USER,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_USER_QUESTION


class AskUserTool(HandlerBase):
    name: str = TOOL_ASK_USER
    # 照录 Claude Code 的工具描述：删去 Plan mode 一段，codebase → workspace，dialog → card。
    description: str = (
        "Use this tool only when you are blocked on a decision that is genuinely the user's to "
        "make: one you cannot resolve from the request, the workspace, or sensible defaults.\n\n"
        "Usage notes:\n"
        '- Users will always be able to select "Other" to provide custom text input\n'
        "- Use multi_select: true to allow multiple answers to be selected for a question\n"
        "- If you recommend a specific option, make that the first option in the list and add "
        '"(Recommended)" at the end of the label\n\n'
        "Reserve this for decisions where the user's answer changes what you do next — not for "
        "choices with a conventional default or facts you can verify in the workspace yourself. "
        "In those cases pick the obvious option, mention it in your response, and proceed.\n\n"
        "The user may see only the question card: tool results and text you wrote earlier in "
        "this turn may not be on their screen while it is open. Make each question stand on its "
        "own: if it depends on something you found, say so briefly, and name the items you are "
        "asking about in the question itself."
    )
    args_model: type[AskUserArgs] = AskUserArgs
    timeout_seconds: ClassVar[float] = 10.0
    group = TOOL_GROUP_USER_QUESTION

    def execute(
            self, questions: list[dict[str, Any]], execution_context: ToolExecutionContext
    ) -> ToolObservation:
        """登记提问并挂起：展示数据就是问题原样，作答后由决定并入补上回答。"""
        return tool_awaiting_decision(
            self.name,
            execution_context.tool_call_id,
            display_data=self.build_user_question_display(questions),
        )

    def build_user_question_display(self, questions: list[dict[str, Any]]):
        """等待时的展示数据就是问题原样。

        作答后由通用的决定并入补上 ``decision`` 与载荷（``answers`` / ``annotations``，见
        ``prepare_input`` 对 ``reply`` 类决定的结算）；作废时不补，状态与短提示走工具状态通道。
        """
        return {"kind": "user-question", "questions": questions}

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            conversation_presentation="standalone",
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            execution_mode="thread",
            user_decision="reply",
            decision_model=AskUserAnswers,
            decision_lifetime="turn",
        )


def build_ask_user_definition() -> ToolDefinition:
    return AskUserTool().to_definition()
