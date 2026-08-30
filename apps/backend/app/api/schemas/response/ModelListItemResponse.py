"""模型目录中的单个模型响应结构。"""

from pydantic import BaseModel


class ModelListItemResponse(BaseModel):
    """描述一个可展示的模型及其静态能力。"""

    model_name: str
    supports_thinking: bool
    supports_image: bool
    supports_video: bool
    supports_reasoning_effort: bool
