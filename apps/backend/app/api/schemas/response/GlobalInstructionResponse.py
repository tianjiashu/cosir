"""全局指令响应结构。"""

from typing import Literal

from pydantic import BaseModel

from app.service.configuration.instruction_configuration_service import GlobalInstructionDocument


class GlobalInstructionResponse(BaseModel):
    """校验并序列化全局指令响应。

    参数:
        content: 全局指令正文。
        path: 指令文件路径。
        token_length: 正文 token 估算数。
        max_tokens: 允许的最大 token 数。
        effective_on: 生效时机，固定为下一次运行（``next_run``）。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    content: str
    path: str
    token_length: int
    max_tokens: int
    effective_on: Literal["next_run"] = "next_run"

    @staticmethod
    def from_document(document: GlobalInstructionDocument) -> "GlobalInstructionResponse":
        """把全局指令文档投影为 HTTP 响应 schema。

        参数:
            document: 指令服务返回的全局指令文档。

        返回:
            可直接作为响应体返回的 ``GlobalInstructionResponse``；``path`` 统一转为字符串。

        异常:
            无（字段缺失或类型不符由 Pydantic 在构造时抛 ``ValidationError``）。

        副作用:
            无（纯投影，不读写文件或数据库）。
        """

        return GlobalInstructionResponse(
            content=document.content,
            path=str(document.path),
            token_length=document.token_length,
            max_tokens=document.max_tokens,
        )
