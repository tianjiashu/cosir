from typing import Any

from pydantic import BaseModel


class ModelEntryResponse(BaseModel):
    """序列化模型条目响应（下拉数据源 / 条目管理，设计文档 §8.2）。

    字段为前端下拉与条目管理所需的最小投影：包含模型条目标识、归属厂商聚合
    信息（显示名、Key 配置状态）与能力标记；不暴露 ``created_at`` / ``updated_at``
    等审计时间字段（由记录层保留），亦不投影 ``model_id`` / ``display_name`` /
    ``max_context_window``（``GET /models`` 端点未返回，前端不得消费）。

    参数:
        provider_id: 归属厂商标识（int 主键）。
        provider_name: 归属厂商显示名（下拉分组展示用）。
        model_name: 模型路由名（后端注册表纯净名，不含厂商前缀，如 ``deepseek-v4-flash``）。
        supports_thinking: 是否推理模型（支持 thinking 通道）。
        supports_image: 是否支持图像输入。
        supports_video: 是否支持视频输入。
        enabled: 启用开关（禁用厂商下的模型不进入下拉）。
        api_key_configured: 归属厂商的 Key 配置状态（发送前校验依据，§9）。
        reasoning_effort: 模型推理强度能力字典（经 ``ModelCapability.reasoning_effort`` 填充）。
        sort_order: 组内排序权重。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    provider_id: int
    provider_name: str
    model_name: str
    supports_thinking: bool
    supports_image: bool
    supports_video: bool
    enabled: bool
    api_key_configured: bool
    reasoning_effort:dict[str,Any]
    sort_order: int = 0
