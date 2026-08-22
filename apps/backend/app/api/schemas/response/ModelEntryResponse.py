from pydantic import BaseModel

from app.models.model_entry_record import ModelEntryRecord
from app.models.provider_record import ProviderRecord
from app.utils.datetime_utils import to_text


class ModelEntryResponse(BaseModel):
    """序列化模型条目响应（下拉数据源 / 条目管理，设计文档 §8.2）。

    参数:
        model_id: 模型条目标识。
        provider_id: 归属厂商标识。
        provider_name: 归属厂商显示名（下拉分组展示用）。
        model_name: litellm 路由名（带 provider 前缀）。
        display_name: 下拉展示名。
        max_context_window: 上下文窗口（token）。
        supports_thinking: 推理模型标识。
        enabled: 启用开关。
        api_key_configured: 归属厂商的 Key 配置状态（发送前校验依据，§9）。
        sort_order: 组内排序权重。
        created_at: 创建时间文本。
        updated_at: 更新时间文本。

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
    supports_thinking: bool = False
    enabled: bool = True
    api_key_configured: bool = True
    sort_order: int = 0
    created_at: str
    updated_at: str

    @classmethod
    def from_records(
        cls,
        model_entry: ModelEntryRecord,
        provider: ProviderRecord,
        *,
        api_key_configured: bool,
    ) -> "ModelEntryResponse":
        """从模型条目 + 归属厂商记录构造响应模型（注入厂商聚合信息）。

        参数:
            model_entry: 待转换的模型条目记录。
            provider: 条目归属的厂商记录（提供展示名与 Key 状态上下文）。
            api_key_configured: 归属厂商的 Key 配置状态（由
                ``ProviderService`` 聚合）。

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
            enabled=model_entry.enabled,
            api_key_configured=api_key_configured,
            sort_order=model_entry.sort_order,
            created_at=to_text(model_entry.created_at),
            updated_at=to_text(model_entry.updated_at),
        )
