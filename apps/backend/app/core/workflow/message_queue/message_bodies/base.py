"""入队消息体的共同契约：类级元数据、渲染出口与推进语义集中声明。

只声明、不给默认值：``PRIORITY`` / ``DEFAULT_PROJECTED`` / ``WAKE`` 逐类判断（通知多半不唤醒、不
投影），默认值会让新增类型默默继承错误的行为。
"""

from abc import ABC, abstractmethod
from typing import ClassVar, Literal

from openai.types.chat import ChatCompletionUserMessageParam
from pydantic import BaseModel, ConfigDict

from app.models.message_metadata import UserMessageMetadata

TaskMessageKind = Literal[
    "user_input", "agent_message", "system_notice", "background_result", "user_decision"
]

WakeScope = Literal["new_or_active", "active", "none"]
"""一条消息能把 Task 推进到哪一步，``dispatch_policy`` 与挂起判定只读它：

- ``new_or_active``：唤醒挂起的 Run，Task 空闲（终态或尚无 Run）时凭它开新 Run；
- ``active``：只对活动 Run 起作用——驱动中不让它挂起、挂起中唤醒它；Task 空闲时不开新 Run，消息
  随下一次推进并入。用于「等着的东西没了」的告知（单项取消的回执、派生工作被停止、重启）：挂起的
  Agent 可能正等着它，空闲的 Agent 不值得为它跑一轮；
- ``none``：不推进，随下一次推进并入。
"""


class EnqueuedMessageBody(BaseModel, ABC):
    """一条可入队消息的领域正文。

    子类必须给出 ``KIND`` / ``TAG`` / ``PROMPT_GUIDE``（读法；靠属性就能说明自己的类型写 ``None``，见
    ``SystemPromptBuilder._input_protocol_lines``）与三个推进开关：``PRIORITY``（并入 context
    的写入顺序）、``DEFAULT_PROJECTED``（是否投影到前端）、``WAKE``（唤醒范围，见 :data:`WakeScope`；
    同一类型按内容区分的覆盖 :meth:`wake_scope`）。能交接新 Run 的输入（携带 ``launch``、一个 Run
    只消费一条）见 :data:`LaunchInputBody`。
    """

    model_config = ConfigDict(extra="forbid")
    KIND: ClassVar[TaskMessageKind]
    TAG: ClassVar[str]
    PROMPT_GUIDE: ClassVar[str | None]
    PRIORITY: ClassVar[int]
    DEFAULT_PROJECTED: ClassVar[bool]
    WAKE: ClassVar[WakeScope]

    def wake_scope(self) -> WakeScope:
        """这一条的唤醒范围：类型声明的 ``WAKE``，按内容区分的类型覆盖。"""
        return type(self).WAKE

    @abstractmethod
    def render(self) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
        """变成一条带标签的 user 消息；读法由 ``PROMPT_GUIDE`` 声明，不写进正文。"""
