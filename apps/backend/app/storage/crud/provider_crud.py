"""``providers`` 表的纯 CRUD 数据访问层。

单一职责：只提供 ``providers`` 单表的增删改查与 model↔record 转换。

职责边界：
- 负责：provider 单表读写、``ProviderModel``↔``ProviderRecord`` 转换。
- 不负责：Key 是否已配置的状态聚合（由 ``service/provider/
  provider_service`` 负责）、models 表的级联语义（由数据库 FK CASCADE 承担）。

依赖约定：构造时通过 ``main_session_factory()`` 取得主库共享 session 工厂，
必须在 ``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

from sqlalchemy import asc, delete, select, update

from app.models import ProviderRecord
from app.storage.model.provider_model import ProviderModel
from app.storage.store_engines import main_session_factory


class ProviderCrud:
    """``providers`` 表的纯 CRUD。

    仅负责单表读写与 model↔record 转换；所有方法通过共享主库 session
    工厂访问数据库。删除 provider 时其下 models 行由 FK CASCADE 级联清理。
    """

    def __init__(self) -> None:
        """绑定主库共享 session 工厂。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 ``init_storage`` 尚未调用（主库 session 工厂不可用）。

        副作用:
            无（仅复用已初始化的主库 session 工厂）。
        """

        self._session_factory = main_session_factory()

    def create(
        self,
        name: str,
        provider_type: str = "api",
        base_url: str | None = None,
        api_key: str | None = None,
        sort_order: int = 0,
    ) -> ProviderRecord:
        """新建一个模型厂商并落库。

        主键 ``id`` 由存储引擎自增分配；name / base_url / api_key /
        去除首尾空白后存储，空白字符串归一为 None。

        参数:
            name: 厂商显示名（如 "DeepSeek "）；不能为空白，对应 ``providers.name``
                唯一约束。
            provider_type: Provider 能力注册表中的接入类型。
            base_url: 可选自定义接入地址；为空时交 litellm 按前缀内置解析，落库到
                ``providers.base_url``。
            api_key: 可选 API Key 明文（DB 唯一事实来源，本地 SQLite 明文存储）；
                日志与响应不回传明文，落库到 ``providers.api_key``。
            sort_order: 排序权重，默认 0，落库到 ``providers.sort_order``；
                ``enabled`` 默认 True 由列 server_default 填充，不在此入参。

        返回:
            落库成功的 ``ProviderRecord``（含自增分配的 id）。

        异常:
            ValueError: 如果 name 去除首尾空白后为空。
            sqlalchemy.exc.IntegrityError: 如果 name 与既有厂商重名（唯一约束）。
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            向 ``providers`` 表插入一行（``type`` 列由 ``ProviderRecord`` 默认
            ``openai-compatible`` 填充，``created_at`` / ``updated_at`` 由应用层默认值填充）。
        """
        record = ProviderRecord(
            name=name,
            provider_type=provider_type,
            base_url=self._normalize_optional(base_url),
            api_key=self._normalize_optional(api_key),
            sort_order=sort_order,
        )
        with self._session_factory.begin() as session:
            model = record.to_model()
            session.add(model)
            session.flush()
            return ProviderRecord.from_model(model)

    def list_all(self, enabled: bool | None = None) -> list[ProviderRecord]:
        """列出全部厂商，按排序权重、创建时间升序。

        ``enabled`` 为筛选开关：``None`` 返回所有厂商（不区分启用状态）；
        非 ``None`` 时仅返回 ``enabled`` 等于入参值的厂商。

        参数:
            enabled: 可选启用状态筛选。``None``（默认）表示不过滤；``True`` 仅返回
                启用厂商；``False`` 仅返回禁用厂商。

        返回:
            匹配的厂商列表，按 ``sort_order`` 再 ``created_at`` 再 ``provider_id``
            升序；无数据时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            stmt = select(ProviderModel).order_by(
                asc(ProviderModel.sort_order),
                asc(ProviderModel.created_at),
                asc(ProviderModel.id),
            )
            if enabled is not None:
                stmt = stmt.where(ProviderModel.enabled == enabled)
            rows = session.execute(stmt).scalars().all()
        return [ProviderRecord.from_model(row) for row in rows]

    def get(self, provider_id: int) -> ProviderRecord:
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

        with self._session_factory() as session:
            row: ProviderModel | None = session.get(ProviderModel, provider_id)
        if row is None:
            raise KeyError(provider_id)
        return ProviderRecord.from_model(row)

    def update(
        self,
        provider_id: int,
        base_url: str | None = None,
        api_key: str | None = None,
        enabled: bool | None = None,
        sort_order: int | None = None,
    ) -> ProviderRecord:
        """更新厂商字段并刷新更新时间；仅覆盖显式传入的字段。

        ``base_url`` / ``api_key`` 传入非 None 值时覆盖（空字符串
        归一为 None）；置空须显式传 ``""``。本方法仅覆盖显式传入的字段，
        ``name`` / ``type`` 列不在更新范围内（由新建语义保证）。

        参数:
            provider_id: 厂商标识。
            base_url: 可选，新接入地址；传 ``""`` 表示清除。
            api_key: 可选，新 API Key 明文；传 ``""`` 表示清除。
            enabled: 可选，新启用状态。
            sort_order: 可选，新排序权重。

        返回:
            更新后的 ``ProviderRecord``。

        异常:
            KeyError: 如果指定厂商不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            更新 ``providers`` 表中对应行的已传字段与 updated_at。
        """

        values: dict[str, object] = {}
        if base_url is not None:
            values["base_url"] = self._normalize_optional(base_url)
        if api_key is not None:
            values["api_key"] = self._normalize_optional(api_key)
        if enabled is not None:
            values["enabled"] = enabled
        if sort_order is not None:
            values["sort_order"] = sort_order
        with self._session_factory.begin() as session:
            result = session.execute(
                update(ProviderModel).where(ProviderModel.id == provider_id).values(**values)
            )
        # 「校验存在」与「更新」分离导致的 TOCTOU 窗口；rowcount=0 即该行不存在。
        if not result.rowcount:
            raise KeyError(provider_id)
        return self.get(provider_id)

    def delete(self, provider_id: int) -> None:
        """删除单个厂商，其下模型行由 FK CASCADE 级联删除。

        参数:
            provider_id: 厂商标识。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            从 ``providers`` 表删除匹配行并级联删除 ``models`` 表关联行；
            provider_id 不存在时静默无操作。
        """

        with self._session_factory.begin() as session:
            session.execute(delete(ProviderModel).where(ProviderModel.id == provider_id))

    @staticmethod
    def _normalize_optional(value: str | None) -> str | None:
        """把可空文本字段归一为首尾空白去除后的值，空白归一为 None。

        参数:
            value: 原始可空文本。

        返回:
            去除空白后的值；入参为 None 或空白时返回 None。

        异常:
            无。

        副作用:
            无。
        """

        if value is None:
            return None
        normalized = value.strip()
        return normalized or None
