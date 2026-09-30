"""模型目录发现响应。"""

from pydantic import BaseModel


class ModelConfigDiscoveryResponse(BaseModel):
    """返回可用于模型名称下拉列表的模型 ID。

    响应只保留标准模型对象的 ``id``，不把供应商的完整响应、能力猜测或错误正文透传给前端。
    请求失败时由服务层返回空列表，前端可继续使用手动输入。
    """

    models: list[str]
