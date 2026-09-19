"""``models`` 表的纯 CRUD 数据访问层。

单一职责：只提供 ``models`` 单表（模型条目）的增删改查与 model↔record 转换。

职责边界：
- 负责：模型条目单表读写（含 discover 批量导入）、按名查启用模型
  （供 ``ModelResolverService`` 解析主路径）、``ModelEntryModel``↔
  ``ModelEntryRecord`` 转换。
- 不负责：厂商归属校验（FK 约束兜底）、Key 配置状态（service 层）、
  Provider 目录发现（``provider_discover_service``）。

依赖约定：构造时通过 ``main_session_factory()`` 取得主库共享 session 工厂，
必须在 ``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

from typing import Any

from sqlalchemy import asc, delete, select, update

from app.models.model_entry_record import (
    ModelEntryRecord,
    _normalize_required_text,
    _positive_context_window,
)
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
        provider_id: int,
        model_name: str,
        display_name: str,
        max_context_window: int,
        supports_thinking: bool = False,
        supports_image: bool = False,
        supports_video: bool = False,
        enabled: bool = True,
        sort_order: int = 0,
    ) -> ModelEntryRecord:
        """新建一个模型条目并落库。

        主键 ``id`` 由存储引擎自增分配。model_name / display_name 去除首尾
        空白后存储。

        参数:
            provider_id: 归属厂商整数 id（FK，指向 ``providers.id``）。
            model_name: Provider 使用的模型名；
                不能为空白，厂商内唯一。
            display_name: 下拉展示名；不能为空白。
            max_context_window: 上下文窗口（token）；必须为正整数。
            supports_thinking: 推理模型标识，默认 False。
            enabled: 启用开关，默认 True。
            sort_order: 组内排序权重，默认 0。

        返回:
            落库成功的 ``ModelEntryRecord``（含自增分配的 id）。

        异常:
            ValueError: 如果 model_name / display_name 归一后为空或
                max_context_window 非正（由 ``ModelEntryRecord`` 校验器判定后
                经 :meth:`_build_record` 归一抛出）。
            sqlalchemy.exc.IntegrityError: 如果 provider 不存在（FK 约束）或
                同厂商下 model_name 重名（唯一约束）。
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            向 ``models`` 表插入一行。
        """
        now = utc_now()
        record = ModelEntryRecord(
            id=0,
            provider_id=provider_id,
            model_name=model_name,
            display_name=display_name,
            max_context_window=max_context_window,
            created_at=now,
            updated_at=now,
            supports_thinking=supports_thinking,
            supports_image=supports_image,
            supports_video=supports_video,
            enabled=enabled,
            sort_order=sort_order,
        )
        with self._session_factory.begin() as session:
            model = record.to_model()
            session.add(model)
            session.flush()
            return ModelEntryRecord.from_model(model)

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
        now = utc_now()
        records = [
            ModelEntryRecord(
                id=0,
                provider_id=int(entry["provider_id"]),
                model_name=entry["model_name"],
                display_name=entry["display_name"],
                max_context_window=entry["max_context_window"],
                supports_thinking=entry.get("supports_thinking", False),
                supports_image=entry.get("supports_image", False),
                supports_video=entry.get("supports_video", False),
                enabled=entry.get("enabled", True),
                sort_order=entry.get("sort_order", 0),
                created_at=now,
                updated_at=now,
            )
            for entry in entries
        ]
        with self._session_factory.begin() as session:
            session.add_all([record.to_model() for record in records])
        return records

    def get(self, model_id: int) -> ModelEntryRecord:
        """按标识返回单个模型条目。

        参数:
            model_id: 模型条目整数 id。

        返回:
            匹配的 ``ModelEntryRecord``。

        异常:
            KeyError: 如果指定条目不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            row: ModelEntryModel | None = session.get(ModelEntryModel, model_id)
        if row is None:
            raise KeyError(model_id)
        return ModelEntryRecord.from_model(row)

    def list_all(self, enabled: bool | None = None) -> list[ModelEntryRecord]:
        """列出全部模型条目，按厂商、排序权重、创建时间升序。

        ``enabled`` 为筛选开关：``None`` 返回所有模型条目（不区分启用状态）；
        非 ``None`` 时仅返回 ``enabled`` 等于入参值的条目。

        参数:
            enabled: 可选启用状态筛选。``None``（默认）表示不过滤；``True`` 仅返回
                启用条目；``False`` 仅返回禁用条目。

        返回:
            匹配的模型条目列表，按 ``provider_id`` 再 ``sort_order`` 再
            ``created_at`` 再 ``model_id`` 升序；无数据时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            stmt = select(ModelEntryModel).order_by(
                asc(ModelEntryModel.provider_id),
                asc(ModelEntryModel.sort_order),
                asc(ModelEntryModel.created_at),
                asc(ModelEntryModel.id),
            )
            if enabled is not None:
                stmt = stmt.where(ModelEntryModel.enabled == enabled)
            rows = session.execute(stmt).scalars().all()
        return [ModelEntryRecord.from_model(row) for row in rows]

    def list_by_provider(self, provider_id: int) -> list[ModelEntryRecord]:
        """列出某厂商下全部模型条目，按排序权重、创建时间升序。

        参数:
            provider_id: 厂商整数 id。

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
                        asc(ModelEntryModel.id),
                    )
                )
                .scalars()
                .all()
            )
        return [ModelEntryRecord.from_model(row) for row in rows]

    def find_enabled_by_name(self, model_name: str) -> ModelEntryRecord | None:
        """按模型名查找启用中的模型条目（解析链主路径）。

        同名条目跨厂商存在时取排序最靠前的启用行（``provider_id`` /
        ``sort_order`` / ``created_at`` 稳定排序）；是否归属启用厂商由
        service 层解析时判定，本方法只看模型行自身的 ``enabled``。

        参数:
            model_name: Provider 使用的模型名。

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
                        asc(ModelEntryModel.id),
                    )
                    .limit(1)
                )
                .scalars()
                .first()
            )
        return ModelEntryRecord.from_model(row) if row is not None else None

    def update(
        self,
        model_id: int,
        display_name: str | None = None,
        max_context_window: int | None = None,
        supports_thinking: bool | None = None,
        supports_image: bool | None = None,
        supports_video: bool | None = None,
        enabled: bool | None = None,
        sort_order: int | None = None,
    ) -> ModelEntryRecord:
        """更新模型条目字段并刷新更新时间；仅覆盖显式传入的字段。

        参数:
            model_id: 模型条目整数 id。
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
            try:
                values["display_name"] = _normalize_required_text(display_name)
            except ValueError as exc:
                raise ValueError(f"display_name {exc}") from exc
        if max_context_window is not None:
            try:
                values["max_context_window"] = _positive_context_window(max_context_window)
            except ValueError as exc:
                raise ValueError(f"max_context_window {exc}") from exc
        if supports_thinking is not None:
            values["supports_thinking"] = supports_thinking
        if supports_image is not None:
            values["supports_image"] = supports_image
        if supports_video is not None:
            values["supports_video"] = supports_video
        if enabled is not None:
            values["enabled"] = enabled
        if sort_order is not None:
            values["sort_order"] = sort_order
        with self._session_factory.begin() as session:
            result = session.execute(
                update(ModelEntryModel).where(ModelEntryModel.id == model_id).values(**values)
            )
        # 以 UPDATE 影响行数判定存在性，替代前置独立 session 的 self.get()，消除
        # 「校验存在」与「更新」分离导致的 TOCTOU 窗口；rowcount=0 即该行不存在。
        if not result.rowcount:
            raise KeyError(model_id)
        return self.get(model_id)

    def delete(self, model_id: int) -> None:
        """删除单个模型条目。

        参数:
            model_id: 模型条目整数 id。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            从 ``models`` 表删除匹配的行；条目不存在时静默无操作。
        """

        with self._session_factory.begin() as session:
            session.execute(delete(ModelEntryModel).where(ModelEntryModel.id == model_id))
