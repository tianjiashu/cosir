"""后台工作的结论消息：后台终端命令（派生 Task 与 Agent Team 的结论走 Agent 消息汇报）。"""

from typing import ClassVar, Literal

from openai.types.chat import ChatCompletionUserMessageParam

from app.core.workflow.message_queue.message_bodies.base import (
    EnqueuedMessageBody,
    TaskMessageKind,
    WakeScope,
)
from app.core.workflow.message_queue.message_bodies.rendering import escape_tag, render_time
from app.models.message_metadata import UserMessageMetadata


class BackgroundResultBody(EnqueuedMessageBody):
    """后台工作的结论：后台终端命令的结束。

    后台命令对原调用回投一次结论，Task 空闲时开出新 Run（工作属于 Task，登记它的
    Run 可能早已结束）。单项取消的回执只唤醒活动 Run、不开新 Run（Task 空闲时随下一次推进并入）；
    上游收回、删除或编辑撤回则放弃工作，不逐项投递；用户停止 Task 不影响它们。重启通知由 system_notice 承载。
    ``tool_name`` 标明来源，``tool_result`` 是模型读到的结论。占位终态表示结论已交付或
    调用被取消；工作本身成功、失败或超时记在结论里。
    ``placeholder_display_data`` 只供结论并入时写回这次调用的后台占位卡片（终端命令的完整输出等），
    不渲染给模型；没有卡片终态的工作为 ``None``。
    不投影：结论落在原调用的卡片上，终端状态走 snapshot ``terminal_sessions``。
    """

    KIND: ClassVar[TaskMessageKind] = "background_result"
    TAG: ClassVar[str] = "background_result"
    # 属性已说明是哪次调用的后台结论；可信度由前言的通用规则交代。
    PROMPT_GUIDE: ClassVar[str | None] = None
    PRIORITY: ClassVar[int] = 1
    DEFAULT_PROJECTED: ClassVar[bool] = False
    WAKE: ClassVar[WakeScope] = "new_or_active"
    tool_call_id: str
    tool_name: str
    tool_result: str
    # 单项取消的回执为 False：只对活动 Run 起作用，Task 空闲时不凭它开新 Run（见 ``wake_scope``）。
    starts_run: bool = True
    placeholder_status: Literal["completed", "cancelled"] = "completed"
    placeholder_error: str | None = None
    placeholder_display_data: dict[str, object] | None = None
    # 结论产生并回投的时刻，随队列持久化；消费可能晚得多，不能届时重取墙钟。
    ended_at: str

    def wake_scope(self) -> WakeScope:
        return "new_or_active" if self.starts_run else "active"

    def render(self) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
        content = (
            f'<{self.TAG} time="{render_time()}" tool_call_id="{self.tool_call_id}" '
            f'tool_name="{self.tool_name}">\n'
            f"{escape_tag(self.tool_result, self.TAG)}\n</{self.TAG}>"
        )
        return (
            ChatCompletionUserMessageParam(role="user", content=content),
            UserMessageMetadata(kind="background_result", tool_call_id=self.tool_call_id),
        )
