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
from app.utils.datetime_utils import to_text, utc_now


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
        provider_type: str,
        base_url: str | None = None,
        api_key: str | None = None,
        enabled: bool = True,
        sort_order: int = 0,
    ) -> ProviderRecord:
        """新建一个模型厂商并落库。

        主键 ``id`` 由存储引擎自增分配；name / base_url / api_key /
        去除首尾空白后存储，空白字符串归一为 None。

        参数:
            name: 厂商显示名（如 "DeepSeek 官方"）；不能为空白，全局唯一。
            provider_type: 厂商类型（``deepseek`` / ``openai-compatible`` /
                ``anthropic`` / ``ollama`` / ``custom``）；不能为空白。
            base_url: 可选自定义接入地址；为空时交 litellm 按前缀内置解析。
            api_key: 可选 API Key 明文（DB 唯一事实来源，本地 SQLite 明文存储）；
                日志与响应不回传明文。
            enabled: 启用开关，默认 True。
            sort_order: 排序权重，默认 0。

        返回:
            落库成功的 ``ProviderRecord``（含自增分配的 id）。

        异常:
            ValueError: 如果 name 或 provider_type 去除首尾空白后为空。
            sqlalchemy.exc.IntegrityError: 如果 name 与既有厂商重名（唯一约束）。
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            向 ``providers`` 表插入一行。
        """

        normalized_name = name.strip()
        normalized_type = provider_type.strip()
        if not normalized_name:
            raise ValueError("provider name must not be blank")
        if not normalized_type:
            raise ValueError("provider type must not be blank")
        now = utc_now()
        record = ProviderRecord(
            id=0,
            name=normalized_name,
            provider_type=normalized_type,
            created_at=now,
            updated_at=now,
            base_url=self._normalize_optional(base_url),
            api_key=self._normalize_optional(api_key),
            enabled=enabled,
            sort_order=sort_order,
        )
        with self._session_factory.begin() as session:
            model = self._to_model(record)
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
        name: str | None = None,
        provider_type: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        enabled: bool | None = None,
        sort_order: int | None = None,
    ) -> ProviderRecord:
        """更新厂商字段并刷新更新时间；仅覆盖显式传入的字段。

        ``base_url`` / ``api_key`` 传入非 None 值时覆盖（空字符串
        归一为 None）；置空须显式传 ``""``。name / provider_type 去除首尾空白后为空
        时抛出 ValueError。

        参数:
            provider_id: 厂商标识。
            name: 可选，新显示名。
            provider_type: 可选，新厂商类型。
            base_url: 可选，新接入地址；传 ``""`` 表示清除。
            api_key: 可选，新 API Key 明文；传 ``""`` 表示清除。
            enabled: 可选，新启用状态。
            sort_order: 可选，新排序权重。

        返回:
            更新后的 ``ProviderRecord``。

        异常:
            KeyError: 如果指定厂商不存在。
            ValueError: 如果 name 或 provider_type 归一后为空。
            sqlalchemy.exc.IntegrityError: 如果 name 与既有厂商重名（唯一约束）。
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            更新 ``providers`` 表中对应行的已传字段与 updated_at。
        """

        values: dict[str, object] = {"updated_at": to_text(utc_now())}
        if name is not None:
            normalized = name.strip()
            if not normalized:
                raise ValueError("provider name must not be blank")
            values["name"] = normalized
        if provider_type is not None:
            normalized_type = provider_type.strip()
            if not normalized_type:
                raise ValueError("provider type must not be blank")
            # 必须用 ORM 属性名 ``provider_type``（而非列名 ``type``）：SQLAlchemy 的
            # update().values() 在默认 evaluate 同步阶段按属性名写回 identity map，
            # 用列名 ``type`` 会抛 ``InvalidRequestError``（见 _to_model 同类注释）。
            values["provider_type"] = normalized_type
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
                update(ProviderModel)
                .where(ProviderModel.id == provider_id)
                .values(**values)
            )
        # 以 UPDATE 影响行数判定存在性，替代前置独立 session 的 self.get()，消除
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

    @staticmethod
    def _to_model(record: ProviderRecord) -> ProviderModel:
        """把 ``ProviderRecord`` 值对象转换为待插入的 ``ProviderModel`` 行。

        参数:
            record: 厂商值对象。

        返回:
            可直接 ``session.add`` 的 ORM 行（时间字段序列化为文本）。

        异常:
            无。

        副作用:
            无。
        """

        model_kwargs: dict[str, object] = {
            "name": record.name,
            # ProviderModel 的 Python 属性名是 ``provider_type``（mapped_column 把
            # 列名映射为 ``"type"`` 以避开 Python 内置 ``type`` 遮蔽）；这里必须
            # 用属性名而非列名传参，否则 SQLAlchemy 抛
            # ``TypeError: 'type' is an invalid keyword argument for ProviderModel``。
            "provider_type": record.provider_type,
            "base_url": record.base_url,
            "api_key": record.api_key,
            "enabled": record.enabled,
            "sort_order": record.sort_order,
            "created_at": to_text(record.created_at),
            "updated_at": to_text(record.updated_at),
        }
        return ProviderModel(**model_kwargs)


