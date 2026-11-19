"""offer_next_step 工具：Agent 做完事后，把留给用户的一个决定做成回复下方的按钮。

与 ``ask_user`` 的分工：``ask_user`` 是「被卡住、等回答」，登记等待并挂起 Run；本工具是「做完了、给个
建议」，当场成功返回——不登记等待、不挂起、不产生任何后端事实，只有展示数据。按钮何时有效不存
状态：前端只在「这是会话最后一个回合且 Run 正常完成」时画出来，用户发了任何消息它就不再是最后一个
回合；点按钮就是替用户发出标签原文这条普通消息，后端不知道它来自按钮。
"""


from app.core.tool.handler.next_step.offer_next_step_args import (
    NEXT_STEP_ASK_MAX,
    NEXT_STEP_REPLY_MAX,
    OfferNextStepArgs,
)
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_OFFER_NEXT_STEP,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_REPLY_PRESENTATION


class OfferNextStepTool(HandlerBase):
    name: str = TOOL_OFFER_NEXT_STEP
    # 改写自 Claude Code 桌面端 ``wrap_up`` 的工具描述：只留「一个问题加按钮」，去掉回顾与应用内动作。
    description: str = (
        "Offer the user one next step as buttons under your reply. Call it when your work this "
        "turn is done and it leaves the user a decision to make or an obvious follow-up to "
        "start: a choice of approach, or a go-ahead to act on what you found. Skip it when "
        "nothing is pending, for short answers, and for plain conversation. Most replies need "
        "none.\n\n"
        "Clicking a button sends its label to you as the user's next message. This call does not "
        "pause your work or wait for an answer. Call it before you write your answer, in a "
        "response with no other text; after it, write your full answer and do not repeat the "
        "question there.\n\n"
        "If you cannot continue without the user's decision, use `ask_user` instead. Never "
        "repeat an offer the user passed over unless what it acts on has changed."
    )
    args_model: type[OfferNextStepArgs] = OfferNextStepArgs
    group = TOOL_GROUP_REPLY_PRESENTATION

    def execute(
        self, ask: str, replies: list[str], execution_context: ToolExecutionContext
    ) -> ToolObservation:
        # 去掉空白与重复；过长时拒绝而不是截断：标签会原样作为用户的话发回来。
        question = ask.strip()
        labels = list(dict.fromkeys(label.strip() for label in replies if label.strip()))
        if (
            not question
            or len(question) > NEXT_STEP_ASK_MAX
            or not labels
            or any(len(label) > NEXT_STEP_REPLY_MAX for label in labels)
        ):
            return tool_error(
                self.name,
                (
                    f"nothing was offered: `ask` must be 1-{NEXT_STEP_ASK_MAX} characters and "
                    f"each reply a label of 1-{NEXT_STEP_REPLY_MAX} characters."
                ),
                tool_call_id=execution_context.tool_call_id,
                status_hint="参数无效",
            )
        return tool_success(
            self.name,
            "Offered. Now write your full answer to the user; do not repeat the question in it.",
            display_data={"kind": "next-step", "ask": question, "replies": labels},
        )

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            # 按钮本身就是这次调用的呈现；调用行只在失败时出现。
            conversation_presentation="quiet",
            name=self.name,
            group=self.group,
            needs_user_surface=True,
            part_of_reply=True,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
        )


def build_offer_next_step_definition() -> ToolDefinition:
    return OfferNextStepTool().to_definition()
