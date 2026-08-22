from pydantic import BaseModel, Field

from app.api.schemas.request.ModelCreateRequest import ModelCreateRequest


class ModelBulkImportRequest(BaseModel):
    """校验按厂商批量导入模型条目的请求体（discover 勾选 / 手动批量添加）。

    参数:
        models: 待导入条目列表（至少 1 条）；同名既有条目由 service 层跳过
            而非报错（幂等导入语义）。

    返回:
        Pydantic 请求模型。

    异常:
        ValueError: 当 ``models`` 为空列表时抛出（由 pydantic 校验触发）。

    副作用:
        无。
    """

    models: list[ModelCreateRequest] = Field(min_length=1)
