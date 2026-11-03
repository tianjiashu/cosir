"""fork_task 的参数模型：分叉的名字与交给它的任务说明。"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.tool.mult_agent.args.delegate_task_args import MESSAGE_MAX

TITLE_MAX = 20


class ForkTaskArgs(BaseModel):
    """``title`` 成为侧栏里这条分叉对话的标题；``message`` 是交给分叉的那份子任务。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(
        description=(
            "Short name of the forked conversation, shown in the sidebar. "
            f"REQUIRED, at most {TITLE_MAX} characters."
        ),
    )
    message: str = Field(
        description=(
            "The part of the work this fork should do. The fork already has this whole "
            "conversation up to now, so say only what is new: its scope, what to leave to you, "
            f"and what to report back. REQUIRED, at most {MESSAGE_MAX} characters."
        ),
    )

    @model_validator(mode="after")
    def _validate_budget(self) -> "ForkTaskArgs":
        if not self.title.strip() or not self.message.strip():
            raise ValueError("fork_task.empty_input: title 与 message 都不能为空。")
        if len(self.title) > TITLE_MAX:
            raise ValueError(f"fork_task.title_over_budget: 标题不得超过 {TITLE_MAX} 个字符。")
        if len(self.message) > MESSAGE_MAX:
            raise ValueError(
                f"fork_task.message_over_budget: message 不得超过 {MESSAGE_MAX} 个字符。"
            )
        return self
