"""fork_session 的参数模型：分叉的名字与交给它的任务说明。"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.tool.multi_agent.args.delegate_task_args import MESSAGE_MAX

TITLE_MAX = 20


class ForkSessionArgs(BaseModel):
    """``title`` 成为侧栏里这条分叉会话的标题；``message`` 是交给分叉的那份子会话。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(
        description=(
            "Short name of the forked conversation, shown in the sidebar. "
            f"REQUIRED, at most {TITLE_MAX} characters."
        ),
    )
    message: str = Field(
        description=(
            "The complete assignment for this fork. It sees the conversation up to your previous "
            "reply, not the current turn, so state the goal, constraints, necessary new facts, what to leave to you "
            f"and what to report back. REQUIRED, at most {MESSAGE_MAX} characters."
        ),
    )

    writable_root: str | None = Field(
        default=None,
        description=(
            "Optional. An existing directory inside the workspace, relative to the workspace root, "
            "that the fork may write to: its file-writing tools are refused elsewhere and its "
            "commands start there. It can only narrow your own writable area, never widen it. "
            "Omit to give the fork the same writable area as you."
        ),
    )

    @model_validator(mode="after")
    def _validate_budget(self) -> "ForkSessionArgs":
        if not self.title.strip() or not self.message.strip():
            raise ValueError("fork_session.empty_input: title 与 message 都不能为空。")
        if len(self.title) > TITLE_MAX:
            raise ValueError(f"fork_session.title_over_budget: 标题不得超过 {TITLE_MAX} 个字符。")
        if len(self.message) > MESSAGE_MAX:
            raise ValueError(
                f"fork_session.message_over_budget: message 不得超过 {MESSAGE_MAX} 个字符。"
            )
        return self
