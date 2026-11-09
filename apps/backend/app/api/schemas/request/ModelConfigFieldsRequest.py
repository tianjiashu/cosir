"""一个模型的请求字段：新建连接、追加模型与连接测试共用。"""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, field_validator

from app.models.model_config_file import ModelEntry


class ModelConfigFieldsRequest(BaseModel):
    """校验并清洗一个模型的字段；连接字段由外层请求另给。"""

    model_name: str
    context_window_k: int = Field(gt=0)
    supports_thinking: bool = False
    supports_reasoning_effort: bool = False
    supports_image: bool = False

    @field_validator("model_name")
    @classmethod
    def model_name_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("model_name must not be blank")
        return normalized

    def to_entry(self) -> ModelEntry:
        return ModelEntry(**self.model_dump(include=set(ModelEntry.model_fields)))


def _unique_model_names(models: list[ModelConfigFieldsRequest]) -> list[ModelConfigFieldsRequest]:
    """同一连接下模型名唯一：一批里有重名就在请求边界拒绝，比写入时整批报冲突更早、更好懂。"""

    names = [model.model_name for model in models]
    if len(set(names)) != len(names):
        raise ValueError("models must not contain duplicate model_name")
    return models


ModelConfigFieldsBatch = Annotated[
    list[ModelConfigFieldsRequest], Field(min_length=1), AfterValidator(_unique_model_names)
]
"""一批要写入同一连接的模型：至少一个、互不重名。"""
