"""模型厂商领域服务（配置状态聚合 + CRUD 编排入口）。

单一职责：面向上层（API / resolver）提供厂商配置的增删改查与
``api_key_configured`` 状态聚合；DB 读写委托 ``ProviderCrud``；关键状态变更
（创建/更新/删除）写 info 日志（设计文档 §7.1）。

职责边界：
- 负责：厂商读写、Key 配置状态判定（DB 是 Key 唯一事实来源，``providers.
  api_key`` 明文列，不做加密）。
- 不负责：模型条目管理（``model_entry_service``）、litellm 目录发现
  （``provider_discover_service``）、模型解析（``model_resolver_service``）。

``api_key_configured`` 语义（设计文档 §8.4）：不依赖 Key 的厂商类型
（``ollama`` 等本地厂商）恒为 True；其余类型检查 ``providers.api_key``
非空。前端只对 False 拦截。Key 明文只允许经 ``LLMRuntimeConfig`` 进入
``factory.build_chat_model``，不进日志 / 事件 / API 响应。

2026-08-18 重构：``_KEYLESS_PROVIDER_TYPES`` 硬编码集合删除，Key 配置需求
改由 ``ProviderCapability.requires_api_key`` 决定（注册表 §三 单一事实源）。
新增厂商无需改本文件——只改注册表一行即可。
"""

from app.config.logging.logger import log
from app.models import ProviderRecord
from app.models.provider_capability import get_capability
from app.service import depends as service_depends
from app.storage.crud.provider_crud import ProviderCrud


class ProviderService:
    """厂商配置读取与 Key 状态聚合服务。"""

    def __init__(self) -> None:
        """初始化厂商 service。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 storage 尚未初始化。

        副作用:
            从 service 依赖入口取得 ProviderCrud 单例并保存引用。
        """

        self._provider_crud: ProviderCrud = service_depends.get_provider_crud()

    def list_providers(self) -> list[ProviderRecord]:
        """列出全部厂商，按排序权重升序。

        参数:
            无。

        返回:
            全部厂商列表（含禁用项；启用过滤由前端 / API 投影决定）。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        return self._provider_crud.list_all()

    def get_provider(self, provider_id: str) -> ProviderRecord:
        """按标识返回单个厂商。

        参数:
            provider_id: 厂商标识。

        返回:
            匹配的 ``ProviderRecord``。

        异常:
            KeyError: 如果指定厂商不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        return self._provider_crud.get(provider_id)

    @staticmethod
    def api_key_configured(provider: ProviderRecord) -> bool:
        """判定厂商的 API Key 是否已配置（DB 唯一事实来源的存在性检查）。

        Key 需求由 ``ProviderCapability.requires_api_key`` 决定（注册表 §三
        单一事实源）；``requires_api_key=False`` 的厂商（如 ollama 本地推理）
        恒返回 True；其余类型以 ``providers.api_key`` 非空为已配置。

        参数:
            provider: 厂商记录。

        返回:
            厂商类型不需要 Key 或 ``api_key`` 非空时返回 True；其余返回
            False。

        异常:
            无。

        副作用:
            无（只读厂商记录内存字段与静态注册表，不读进程环境变量）。
        """

        capability = get_capability(provider.provider_type)
        if not capability.requires_api_key:
            return True
        return bool(provider.api_key)

    def create_provider(
        self,
        name: str,
        provider_type: str,
        base_url: str | None = None,
        api_key: str | None = None,
        enabled: bool = True,
        sort_order: int = 0,
    ) -> ProviderRecord:
        """新建模型厂商并写 ``provider_created`` 审计日志。

        参数:
            name: 厂商显示名（全局唯一）。
            provider_type: 厂商类型（``deepseek`` / ``openai-compatible`` /
                ``anthropic`` / ``ollama`` / ``custom``）。
            base_url: 可选自定义接入地址；为空时交 litellm 内置解析。
            api_key: 可选 API Key 明文（DB 唯一事实来源，本地 SQLite 明文存储；
                日志与响应不回传明文）。
            enabled: 启用开关，默认 True。
            sort_order: 排序权重，默认 0。

        返回:
            落库成功的 ``ProviderRecord``。

        异常:
            ValueError: 如果 name / provider_type 为空白。
            sqlalchemy.exc.IntegrityError: 如果 name 与既有厂商重名。
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            向 ``providers`` 表插入一行；写 info 日志 ``provider_created``。
        """

        record = self._provider_crud.create(
            name=name,
            provider_type=provider_type,
            base_url=base_url,
            api_key=api_key,
            enabled=enabled,
            sort_order=sort_order,
        )
        log.info(
            "provider_created",
            extra={
                "msg": f"模型厂商已创建：{record.name}",
                "data": {
                    "provider_id": record.provider_id,
                    "name": record.name,
                    "type": record.provider_type,
                    "enabled": record.enabled,
                },
            },
        )
        return record

    def update_provider(
        self,
        provider_id: str,
        *,
        name: str | None = None,
        provider_type: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        enabled: bool | None = None,
        sort_order: int | None = None,
    ) -> ProviderRecord:
        """更新厂商字段并写 ``provider_updated`` 审计日志。

        仅覆盖显式传入的字段；``api_key`` 传 None 表示不更新（保留原值），
        传 ``""`` 表示清除；启停即时生效（下次模型解析实时查 DB）。

        参数:
            provider_id: 厂商标识。
            name: 可选，新显示名。
            provider_type: 可选，新厂商类型。
            base_url: 可选，新接入地址；传 ``""`` 表示清除。
            api_key: 可选，新 API Key 明文；传 ``None`` 不更新、传 ``""``
                清除（日志与响应不回传明文）。
            enabled: 可选，新启用状态。
            sort_order: 可选，新排序权重。

        返回:
            更新后的 ``ProviderRecord``。

        异常:
            KeyError: 如果指定厂商不存在。
            ValueError: 如果 name / provider_type 归一后为空。
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            更新 ``providers`` 表对应行；写 info 日志 ``provider_updated``。
        """

        provided = {
            key: value
            for key, value in (
                ("name", name),
                ("type", provider_type),
                ("base_url", base_url),
                ("api_key", api_key),
                ("enabled", enabled),
                ("sort_order", sort_order),
            )
            if value is not None
        }
        record = self._provider_crud.update(
            provider_id,
            name=name,
            provider_type=provider_type,
            base_url=base_url,
            api_key=api_key,
            enabled=enabled,
            sort_order=sort_order,
        )
        log.info(
            "provider_updated",
            extra={
                "msg": f"模型厂商已更新：{record.name}",
                "data": {
                    "provider_id": provider_id,
                    "updated_fields": sorted(provided.keys()),
                    "enabled": record.enabled,
                },
            },
        )
        return record

    def delete_provider(self, provider_id: str) -> None:
        """删除厂商（级联删其下模型）并写 ``provider_deleted`` 审计日志。

        参数:
            provider_id: 厂商标识。

        返回:
            无。

        异常:
            无。厂商不存在时静默无操作（幂等删除语义）。

        副作用:
            从 ``providers`` 表删除匹配行并级联删除 ``models`` 关联行；
            写 info 日志 ``provider_deleted``。
        """

        try:
            record = self._provider_crud.get(provider_id)
        except KeyError:
            record = None
        self._provider_crud.delete(provider_id)
        log.info(
            "provider_deleted",
            extra={
                "msg": "模型厂商已删除（模型条目级联清理）",
                "data": {
                    "provider_id": provider_id,
                    "name": record.name if record else None,
                },
            },
        )
