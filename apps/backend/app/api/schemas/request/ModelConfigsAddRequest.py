"""给已有连接追加模型的请求。"""

from pydantic import BaseModel

from app.api.schemas.request.ModelConfigFieldsRequest import ModelConfigFieldsBatch


class ModelConfigsAddRequest(BaseModel):
    """一批要追加的模型；已在该连接下的模型名由存储唯一约束拒绝（409）。"""

    models: ModelConfigFieldsBatch
