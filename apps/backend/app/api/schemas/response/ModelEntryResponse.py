from pydantic import BaseModel

from app.models.model_entry_record import ModelEntryRecord
from app.models.provider_record import ProviderRecord
from app.utils.datetime_utils import to_text


class ModelEntryResponse(BaseModel):
    """序列化模型条目响应（下拉数据源 / 条目管理，设计文档 §8.2）。

    字段为前端下拉与条目管理所需的最小投影：包含模型条目标识、归属厂商聚合
    信息（显示名、Key 配置状态）与能力标记；不暴露 ``created_at`` / ``updated_at``
    等审计时间字段（由记录层保留）。

    参数:
        model_id: 模型条目标识。
        provider_id: 归属厂商标识。
        provider_name: 归属厂商显示名（下拉分组展示用）。
        model_name: litellm 路由名（带 provider 前缀，如 ``deepseek/deepseek-v4-flash``）。
        display_name: 下拉展示名（用户可读，可与 model_name 不同）。
        max_context_window: 上下文窗口上限（token）。
        supports_thinking: 是否推理模型（支持 thinking 通道）。
        supports_image: 是否支持图像输入。
        supports_video: 是否支持视频输入。
        api_key_configured: 归属厂商的 Key 配置状态（发送前校验依据，§9）。
        sort_order: 组内排序权重。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    model_id: str
    provider_id: str
    provider_name: str
    model_name: str
    display_name: str
    max_context_window: int
    supports_thinking: bool
    supports_image: bool
    supports_video: bool
    enabled: bool
    api_key_configured: bool
    sort_order: int = 0

    @classmethod
    def from_records(
        cls,
        model_entry: ModelEntryRecord,
        provider: ProviderRecord,
        *,
        api_key_configured: bool,
    ) -> "ModelEntryResponse":
        """从模型条目 + 归属厂商记录构造响应模型（注入厂商聚合信息）。

        仅投影前端下拉 / 条目管理所需的字段：模型条目字段 + 厂商 ``name`` 与
        外部传入的 ``api_key_configured`` 聚合状态；不投影审计时间字段
        （``ModelEntryRecord`` 的 ``created_at`` / ``updated_at``）。

        参数:
            model_entry: 待转换的模型条目记录（提供除厂商聚合外的全部字段）。
            provider: 条目归属的厂商记录（提供 ``name`` 展示名）。
            api_key_configured: 归属厂商的 Key 配置状态（由 ``ProviderService``
                聚合后传入，发送前校验依据）。

        返回:
            与记录字段对齐并带厂商聚合信息的 ``ModelEntryResponse`` 实例。

        异常:
            无。

        副作用:
            无。
        """

        return cls(
            model_id=model_entry.model_id,
            provider_id=model_entry.provider_id,
            provider_name=provider.name,
            model_name=model_entry.model_name,
            display_name=model_entry.display_name,
            max_context_window=model_entry.max_context_window,
            supports_thinking=model_entry.supports_thinking,
            supports_image=model_entry.supports_image,
            supports_video=model_entry.supports_video,
            enabled=model_entry.enabled,
            api_key_configured=api_key_configured,
            sort_order=model_entry.sort_order,
        )
