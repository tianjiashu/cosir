from pydantic import BaseModel


class HealthResponse(BaseModel):
    """校验并序列化后端健康状态响应（与 ``AgentRuntime.backend_health()`` 对齐）。

    参数:
        status: 健康状态标识。
        model_provider: 模型提供方。
        model_base_url: 模型基础地址。
        model_name: 模型名称。
        model_thinking_mode: 模型思考模式。
        model_api_key_env: API Key 所在环境变量名。
        has_model_api_key: 环境变量是否已配置。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    status: str
    model_provider: str
    model_base_url: str
    model_name: str
    model_thinking_mode: str
    model_api_key_env: str
    has_model_api_key: bool
