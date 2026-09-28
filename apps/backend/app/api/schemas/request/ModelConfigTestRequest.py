"""未保存模型配置测试请求。"""

from pydantic import BaseModel


class ModelConfigTestRequest(BaseModel):
    """承载一次不落库的真实 OpenAI 兼容连接测试参数。"""

    base_url: str
    api_key: str
    model_name: str
