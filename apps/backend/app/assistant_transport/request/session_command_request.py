"""Session 级命令的 wire schema：发送消息与 HITL 决定（撤回、取消等命令没有请求体）。

命令只对 Session（路径中的 ``session_id``）或消息（路径中的 ``message_id``）寻址，不携带 run_id：Run
身份由后端按 Session 的最新 Run 解析。wire 字段沿用 camelCase；``SendMessageRequest.to_run_command``
把消息正文映射为领域命令，发起配置字段由应用服务直接读取。
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.assistant_transport.request.assistant_user_message import (
    AssistantImagePart,
    AssistantTextPart,
    AssistantUserMessage,
)
from app.models import RunCommand
from app.models.enums.reasoning_effort import ReasoningEffort


class SendMessageRequest(BaseModel):
    """发送消息；并入时机不由请求决定：后端按 Session 是否有活动 Run 定排队还是立即处理。

    发起配置随消息携带，只在这条消息开新 Run 时生效（见 ``RunLaunch``）。
    """

    model_config = ConfigDict(extra="forbid")

    message: AssistantUserMessage
    # 模型名由后端配置事实读取，请求只携带配置身份。
    modelConfigId: str = Field(min_length=1)
    reasoningEffort: ReasoningEffort | None = None

    def to_run_command(self) -> RunCommand:
        """text part 按出现顺序用换行连成胶囊正文；图片只携带已上传附件 id。"""

        return RunCommand(
            text="\n".join(
                part.text for part in self.message.parts if isinstance(part, AssistantTextPart)
            ),
            image_asset_ids=list(
                dict.fromkeys(
                    part.image.removeprefix("cosir-attachment://")
                    for part in self.message.parts
                    if isinstance(part, AssistantImagePart)
                )
            ),
        )


class DecisionRequest(BaseModel):
    """对一次等待用户决定的工具调用（HITL）作出决定；调用由路径里的 ``tool_call_id`` 寻址。

    ``data`` 是工具声明的结构化回答（``ToolDefinition.decision_model``），这里只收原样 JSON，
    形状由决定入口按工具校验（400 ``DECISION_DATA_INVALID``）。
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    feedback: str | None = None
    data: dict[str, Any] | None = None
