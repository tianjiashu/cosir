"""``models`` 表的纯 CRUD 数据访问层。

单一职责：只提供 ``models`` 单表（模型条目）的增删改查与 model↔record 转换。

职责边界：
- 负责：模型条目单表读写（含 discover 批量导入）、按名查启用模型
  （供 ``ModelResolverService`` 解析主路径）、``ModelEntryModel``↔
  ``ModelEntryRecord`` 转换。
- 不负责：厂商归属校验（FK 约束兜底）、Key 配置状态（service 层）、
  litellm 目录发现（``provider_discover_service``）。

依赖约定：构造时通过 ``main_session_factory()`` 取得主库共享 session 工厂，
必须在 ``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

from typing import Any
from uuid import uuid4

from sqlalchemy import asc, delete, select, update

from app.models import ModelEntryRecord
from app.storage.model.model_entry_model import ModelEntryModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import to_text, utc_now


class ModelEntryCrud:
    """``models`` 表的纯 CRUD。

    仅负责单表读写与 model↔record 转换；所有方法通过共享主库 session
    工厂访问数据库。
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
        provider_id: str,
        model_name: str,
        display_name: str,
        max_context_window: int,
        supports_thinking: bool = False,
        enabled: bool = True,
        sort_order: int = 0,
    ) -> ModelEntryRecord:
        """新建一个模型条目并落库。

        ``model_id`` 由本方法生成（UUID4）；model_name / display_name 去除首尾
        空白后存储。

        参数:
            provider_id: 归属厂商标识（FK）。
            model_name: litellm 路由名（如 ``deepseek/deepseek-v4-flash``）；
                不能为空白，厂商内唯一。
            display_name: 下拉展示名；不能为空白。
            max_context_window: 上下文窗口（token）；必须为正整数。
            supports_thinking: 推理模型标识，默认 False。
            enabled: 启用开关，默认 True。
            sort_order: 组内排序权重，默认 0。

        返回:
            落库成功的 ``ModelEntryRecord``。

        异常:
            ValueError: 如果 model_name / display_name 归一后为空或
                max_context_window 非正。
            sqlalchemy.exc.IntegrityError: 如果 provider 不存在（FK 约束）或
                同厂商下 model_name 重名（唯一约束）。
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            向 ``models`` 表插入一行。
        """

        normalized_name = model_name.strip()
        normalized_display = display_name.strip()
        if not normalized_name:
            raise ValueError("model_name must not be blank")
        if not normalized_display:
            raise ValueError("display_name must not be blank")
        if max_context_window <= 0:
            raise ValueError("max_context_window must be positive")
        record = self._build_record(
            provider_id=provider_id,
            model_name=normalized_name,
            display_name=normalized_display,
            max_context_window=max_context_window,
            supports_thinking=supports_thinking,
            enabled=enabled,
            sort_order=sort_order,
        )
        with self._session_factory.begin() as session:
            session.add(self._to_model(record))
        return record

    def bulk_create(self, entries: list[dict[str, Any]]) -> list[ModelEntryRecord]:
        """批量新建模型条目（discover 勾选导入路径），单事务写入。

        任一条目违反约束（重名 / FK / 空白字段）时整批回滚，不产生半批入库。

        参数:
            entries: 待导入条目的字段字典列表，每项字段与 ``create`` 的关键字
                参数一致（provider_id / model_name / display_name /
                max_context_window / supports_thinking / enabled / sort_order）；
                为空时直接返回空列表。

        返回:
            落库成功的 ``ModelEntryRecord`` 列表，顺序与入参一致。

        异常:
            ValueError: 如果任一条目字段非法（空白 / 窗口非正）。
            sqlalchemy.exc.IntegrityError: 如果整批中存在重名或 FK 违约。
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            向 ``models`` 表批量插入行（单事务）。
        """

        if not entries:
            return []
        records = [
            self._build_record(
                provider_id=entry["provider_id"],
                model_name=entry["model_name"],
                display_name=entry["display_name"],
                max_context_window=entry["max_context_window"],
                supports_thinking=entry.get("supports_thinking", False),
                enabled=entry.get("enabled", True),
                sort_order=entry.get("sort_order", 0),
            )
            for entry in entries
        ]
        with self._session_factory.begin() as session:
            session.add_all([self._to_model(record) for record in records])
        return records

    def get(self, model_id: str) -> ModelEntryRecord:
        """按标识返回单个模型条目。

        参数:
            model_id: 模型条目标识。

        返回:
            匹配的 ``ModelEntryRecord``。

        异常:
            KeyError: 如果指定条目不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            row = session.get(ModelEntryModel, model_id)
        if row is None:
            raise KeyError(model_id)
        return ModelEntryRecord.from_model(row)

    def list_all(self) -> list[ModelEntryRecord]:
        """列出全部模型条目，按厂商、排序权重、创建时间升序。

        参数:
            无。

        返回:
            全部模型条目列表，按 ``provider_id`` 再 ``sort_order`` 再
            ``created_at`` 再 ``model_id`` 升序；无数据时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(ModelEntryModel).order_by(
                        asc(ModelEntryModel.provider_id),
                        asc(ModelEntryModel.sort_order),
                        asc(ModelEntryModel.created_at),
                        asc(ModelEntryModel.model_id),
                    )
                )
                .scalars()
                .all()
            )
        return [ModelEntryRecord.from_model(row) for row in rows]

    def list_by_provider(self, provider_id: str) -> list[ModelEntryRecord]:
        """列出某厂商下全部模型条目，按排序权重、创建时间升序。

        参数:
            provider_id: 厂商标识。

        返回:
            该厂商的模型条目列表；厂商无模型或不存在时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(ModelEntryModel)
                    .where(ModelEntryModel.provider_id == provider_id)
                    .order_by(
                        asc(ModelEntryModel.sort_order),
                        asc(ModelEntryModel.created_at),
                        asc(ModelEntryModel.model_id),
                    )
                )
                .scalars()
                .all()
            )
        return [ModelEntryRecord.from_model(row) for row in rows]

    def find_enabled_by_name(self, model_name: str) -> ModelEntryRecord | None:
        """按 litellm 路由名查找启用中的模型条目（解析链主路径）。

        同名条目跨厂商存在时取排序最靠前的启用行（``provider_id`` /
        ``sort_order`` / ``created_at`` 稳定排序）；是否归属启用厂商由
        service 层解析时判定，本方法只看模型行自身的 ``enabled``。

        参数:
            model_name: litellm 路由名（如 ``deepseek/deepseek-v4-flash``）。

        返回:
            匹配的启用条目；无匹配时返回 None。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            row = (
                session.execute(
                    select(ModelEntryModel)
                    .where(
                        ModelEntryModel.model_name == model_name,
                        ModelEntryModel.enabled.is_(True),
                    )
                    .order_by(
                        asc(ModelEntryModel.provider_id),
                        asc(ModelEntryModel.sort_order),
                        asc(ModelEntryModel.created_at),
                    )
                    .limit(1)
                )
                .scalars()
                .first()
            )
        return ModelEntryRecord.from_model(row) if row is not None else None

    def update(
        self,
        model_id: str,
        display_name: str | None = None,
        max_context_window: int | None = None,
        supports_thinking: bool | None = None,
        enabled: bool | None = None,
        sort_order: int | None = None,
    ) -> ModelEntryRecord:
        """更新模型条目字段并刷新更新时间；仅覆盖显式传入的字段。

        参数:
            model_id: 模型条目标识。
            display_name: 可选，新展示名。
            max_context_window: 可选，新上下文窗口；必须为正整数。
            supports_thinking: 可选，新推理模型标识。
            enabled: 可选，新启用状态。
            sort_order: 可选，新排序权重。

        返回:
            更新后的 ``ModelEntryRecord``。

        异常:
            KeyError: 如果指定条目不存在。
            ValueError: 如果 display_name 归一后为空或 max_context_window 非正。
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            更新 ``models`` 表中对应行的已传字段与 updated_at。
        """

        values: dict[str, object] = {"updated_at": to_text(utc_now())}
        if display_name is not None:
            normalized = display_name.strip()
            if not normalized:
                raise ValueError("display_name must not be blank")
            values["display_name"] = normalized
        if max_context_window is not None:
            if max_context_window <= 0:
                raise ValueError("max_context_window must be positive")
            values["max_context_window"] = max_context_window
        if supports_thinking is not None:
            values["supports_thinking"] = supports_thinking
        if enabled is not None:
            values["enabled"] = enabled
        if sort_order is not None:
            values["sort_order"] = sort_order
        with self._session_factory.begin() as session:
            result = session.execute(
                update(ModelEntryModel)
                .where(ModelEntryModel.model_id == model_id)
                .values(**values)
            )
        # 以 UPDATE 影响行数判定存在性，替代前置独立 session 的 self.get()，消除
        # 「校验存在」与「更新」分离导致的 TOCTOU 窗口；rowcount=0 即该行不存在。
        if not result.rowcount:
            raise KeyError(model_id)
        return self.get(model_id)

    def delete(self, model_id: str) -> None:
        """删除单个模型条目。

        参数:
            model_id: 模型条目标识。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            从 ``models`` 表删除匹配的行；条目不存在时静默无操作。
        """

        with self._session_factory.begin() as session:
            session.execute(delete(ModelEntryModel).where(ModelEntryModel.model_id == model_id))

    def _build_record(
        self,
        provider_id: str,
        model_name: str,
        display_name: str,
        max_context_window: int,
        supports_thinking: bool,
        enabled: bool,
        sort_order: int,
    ) -> ModelEntryRecord:
        """组装待落库的 ``ModelEntryRecord``（生成 UUID 与时间戳）。

        参数:
            provider_id: 归属厂商标识。
            model_name: 已归一的 litellm 路由名。
            display_name: 已归一的展示名。
            max_context_window: 已校验的上下文窗口。
            supports_thinking: 推理模型标识。
            enabled: 启用开关。
            sort_order: 排序权重。

        返回:
            未落库的 ``ModelEntryRecord``（调用方负责写入）。

        异常:
            ValueError: 如果 model_name / display_name 为空或窗口非正
                （批量路径集中校验，保证整批回滚语义前置）。

        副作用:
            无。
        """

        if not str(model_name).strip():
            raise ValueError("model_name must not be blank")
        if not str(display_name).strip():
            raise ValueError("display_name must not be blank")
        window = int(max_context_window)
        if window <= 0:
            raise ValueError("max_context_window must be positive")
        now = utc_now()
        return ModelEntryRecord(
            model_id=str(uuid4()),
            provider_id=str(provider_id),
            model_name=str(model_name).strip(),
            display_name=str(display_name).strip(),
            max_context_window=window,
            created_at=now,
            updated_at=now,
            supports_thinking=bool(supports_thinking),
            enabled=bool(enabled),
            sort_order=int(sort_order),
        )

    @staticmethod
    def _to_model(record: ModelEntryRecord) -> ModelEntryModel:
        """把 ``ModelEntryRecord`` 值对象转换为待插入的 ``ModelEntryModel`` 行。

        参数:
            record: 模型条目值对象。

        返回:
            可直接 ``session.add`` 的 ORM 行（时间字段序列化为文本）。

        异常:
            无。

        副作用:
            无。
        """

        return ModelEntryModel(
            model_id=record.model_id,
            provider_id=record.provider_id,
            model_name=record.model_name,
            display_name=record.display_name,
            max_context_window=record.max_context_window,
            supports_thinking=record.supports_thinking,
            enabled=record.enabled,
            sort_order=record.sort_order,
            created_at=to_text(record.created_at),
            updated_at=to_text(record.updated_at),
        )


