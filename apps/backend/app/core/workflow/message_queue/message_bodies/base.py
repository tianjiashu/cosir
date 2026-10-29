"""入队消息体的共同契约：类级元数据、渲染出口与推进语义集中声明。

只声明、不给默认值：``PRIORITY`` / ``DEFAULT_PROJECTED`` / ``WAKES`` 逐类判断（通知不唤醒、不
投影），默认值会让新增类型默默继承错误的行为。
"""

from abc import ABC, abstractmethod
from typing import ClassVar, Literal

from openai.types.chat import ChatCompletionUserMessageParam
from pydantic import BaseModel, ConfigDict

from app.models.message_metadata import UserMessageMetadata

TaskMessageKind = Literal["user_input", "system_notice", "background_result", "user_decision"]


class EnqueuedMessageBody(BaseModel, ABC):
    """一条可入队消息的领域正文。

    子类必须给出 ``KIND`` / ``TAG`` / ``PROMPT_GUIDE`` 与三个推进开关：``PRIORITY``（并入 context
    的写入顺序）、``DEFAULT_PROJECTED``（是否投影到前端）、``WAKES``（能否单独推进 Run）。
    """

    model_config = ConfigDict(extra="forbid")
    KIND: ClassVar[TaskMessageKind]
    TAG: ClassVar[str]
    PROMPT_GUIDE: ClassVar[str]
    PRIORITY: ClassVar[int]
    DEFAULT_PROJECTED: ClassVar[bool]
    WAKES: ClassVar[bool]

    @abstractmethod
    def render(self) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
        """变成一条带标签的 user 消息；读法由 ``PROMPT_GUIDE`` 声明，不写进正文。"""
