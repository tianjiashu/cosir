"""Assistant Transport attach 请求的 wire schema。

本模块只描述桌面端重新订阅已有 Conversation Run 的 transport 请求结构，不依赖领域模型、
LangGraph 或 storage。wire 字段遵循 assistant-ui 的 camelCase 约定；attach 请求只消费身份
字段（threadId / taskId / runId / commands），并显式忽略 assistant-ui 的通用 transport
envelope（state / system / tools / callSettings / config）。
"""

from pydantic import BaseModel, ConfigDict, Field


class AssistantAttachRequest(BaseModel):
    """只订阅已有 Conversation Run 的 transport 请求。

    attach 请求不携带业务命令（由 ``assistant_transport_attach`` 端点校验拒绝），仅用于
    重新订阅一个已存在、可能仍在运行的 Run 的 SSE 流。为兼容 assistant-ui resume 请求的
    通用 transport envelope，``commands`` 字段允许为空并被显式忽略。
    """

    # assistant-ui resume requests carry the common transport envelope
    # (commands/state/system/tools/callSettings/config). Attach only consumes
    # identity fields and intentionally ignores that envelope.
    model_config = ConfigDict(extra="ignore")

    commands: list[object] = Field(default_factory=list)
    taskId: int | None = Field(default=None, ge=1)
    threadId: str = Field(pattern=r"^task-[1-9][0-9]*$")
    runId: int = Field(ge=1)
