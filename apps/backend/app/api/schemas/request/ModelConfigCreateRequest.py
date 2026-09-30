"""模型连接配置创建请求。"""

from app.api.schemas.request.ModelConfigFieldsRequest import ModelConfigFieldsRequest


class ModelConfigCreateRequest(ModelConfigFieldsRequest):
    """校验创建模型连接配置所需的完整表单字段。"""

    sort_order: int = 0
