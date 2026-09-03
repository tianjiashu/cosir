from typing import ClassVar

from pydantic import BaseModel, field_validator

from app.models.attachment_ref import AttachmentRef


class CreateTurnRequest(BaseModel):
    """校验轮次创建请求体。

    参数:
        input_text: 非空的本轮用户输入。
        provider_id: 可选，模型归属的厂商标识（指向 ``providers.id``）；None 表示未选厂商。
        model_name: 可选，本 turn 关联的模型名；None 表示未选模型。
        attachments: 可选，本 turn 携带的附件引用列表（结构化，含类型与引用）。
            仅做结构合法性校验；workspace 边界、视觉能力等业务规则在 service 层判定。
        reasoning_effort: 可选，本 turn 思考努力等级；None 表示 max。该值经
            ``conversation_run_state_service.create_run`` 透传落库到 ``turns.reasoning_effort``。

    返回:
        Pydantic 请求模型。

    异常:
        ValueError: 当输入为空白或附件列表非法时抛出。

    副作用:
        无。本层只做结构校验，不访问文件系统、不做业务规则判定。
    """

    MAX_ATTACHMENTS: ClassVar[int] = 20
    MAX_REF_LEN: ClassVar[int] = 4096

    input_text: str
    model_name: str | None = None
    provider_id: int | None = None
    attachments: list[AttachmentRef] | None = None
    reasoning_effort: str | None = None

    @field_validator("input_text")
    @classmethod
    def input_text_must_not_be_blank(cls, value: str) -> str:
        """校验本轮输入不为空白。

        参数:
            value: 从请求体解析出的输入文本。

        返回:
            去除首尾空白后的输入文本。

        异常:
            ValueError: 当输入为空白时抛出。

        副作用:
            无。
        """

        if not value or not value.strip():
            raise ValueError("input_text must not be blank")
        value = value.strip()
        if len(value) > 1000:
            raise ValueError("input_text must be less than 1000 characters")
        return value

    @field_validator("attachments")
    @classmethod
    def attachments_validator(
        cls, attachments: list[AttachmentRef] | None
    ) -> list[AttachmentRef] | None:
        """校验附件列表的结构合法性与预算约束。

        仅做本层可判的结构校验：非空项、项数上限、单条引用长度上限；
        url 类型必须显式为 http(s)。文件系统语义（是否存在、是否图片）
        由 service 层访问磁盘判定，不在此处。

        参数:
            attachments: 从请求体解析出的附件列表。

        返回:
            校验通过的附件列表。

        异常:
            ValueError: 当列表含空白项、项数超过上限、单条引用超长或 url 缺少
                http(s) 方案时抛出。

        副作用:
            无。
        """

        if attachments is None:
            return attachments
        if not attachments:
            raise ValueError("attachments must not be empty")
        if len(attachments) > cls.MAX_ATTACHMENTS:
            raise ValueError("attachments must be less than 20 items")
        for att in attachments:
            if not att.ref or not att.ref.strip():
                raise ValueError("attachment ref must not be blank")
            if len(att.ref) > cls.MAX_REF_LEN:
                raise ValueError("attachment ref must be less than 4096 characters")
            if att.kind == "url" and not att.ref.lower().startswith(("http://", "https://")):
                raise ValueError("url attachment must start with http(s)://")
        return attachments
