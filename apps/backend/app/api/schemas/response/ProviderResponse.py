from pydantic import BaseModel

from app.models.provider_record import ProviderRecord


class ProviderResponse(BaseModel):
    """序列化厂商配置响应（含聚合状态，设计文档 §8.1）。

    参数:
        provider_id: 厂商标识。
        name: 厂商名称。
        base_url: 自定义接入地址（可空，空时 litellm 内置解析）。
        api_key_configured: Key 配置状态（不依赖 Key 的厂商类型恒 True，§8.4；
            其余以 ``providers.api_key`` 非空为准）。响应永不回传 Key 明文。
        enabled: 启用开关。
        model_count: 该厂商下模型条目数（含禁用条目）。
        sort_order: 排序权重。
        created_at: 创建时间文本。
        updated_at: 更新时间文本。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    provider_id: int
    name: str
    base_url: str | None = None
    api_key_configured: bool = True
    enabled: bool = True
    model_count: int = 0
    sort_order: int = 0
    created_at: str
    updated_at: str

    @classmethod
    def from_record(
        cls,
        record: ProviderRecord,
        *,
        api_key_configured: bool,
        model_count: int = 0,
    ) -> "ProviderResponse":
        """从 ``ProviderRecord`` 值对象构造响应模型（注入聚合状态）。

        参数:
            record: 待转换的厂商记录。
            api_key_configured: Key 配置状态（由 ``ProviderService`` 聚合）。
            model_count: 该厂商下模型条目数（由 API 层聚合查询）。

        返回:
            与记录字段对齐并带聚合状态的 ``ProviderResponse`` 实例。

        异常:
            无。

        副作用:
            无。
        """

        if record.id is None:
            raise ValueError("ProviderRecord.id 不能为空")

        return cls(
            provider_id=record.id,
            name=record.name,
            base_url=record.base_url,
            api_key_configured=api_key_configured,
            enabled=record.enabled,
            model_count=model_count,
            sort_order=record.sort_order,
            created_at=record.created_at.isoformat() if record.created_at else "",
            updated_at=record.updated_at.isoformat() if record.updated_at else "",
        )
