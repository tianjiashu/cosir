"""未保存模型配置测试请求。"""

from app.api.schemas.request.ModelConfigFieldsRequest import ModelConfigFieldsRequest


class ModelConfigTestRequest(ModelConfigFieldsRequest):
    """承载完整表单的一次不落库 OpenAI 兼容连接测试请求。

    连接测试不写入数据库，但仍要求提交与保存相同的完整字段，确保前端只能在表单完整
    时发起测试，并让测试使用与最终保存一致的上下文窗口和能力声明。
    """
