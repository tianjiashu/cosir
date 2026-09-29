"""模型连接配置单表 CRUD。"""

from sqlalchemy import asc, delete, select, update

from app.models.model_config_record import ModelConfigRecord
from app.storage.model.model_config_model import ModelConfigModel
from app.storage.store_engines import main_session_factory


class ModelConfigCrud:
    """只负责 ``model_configs`` 表的读写，不承担网络测试和业务编排。"""

    def __init__(self) -> None:
        """绑定已经初始化的主库 session 工厂。"""

        self._session_factory = main_session_factory()

    def create(
        self,
        *,
        config_name: str,
        base_url: str,
        api_key: str,
        model_name: str,
        context_window_k: int,
        supports_thinking: bool = False,
        supports_reasoning_effort: bool = False,
        supports_image: bool = False,
        sort_order: int = 0,
    ) -> ModelConfigRecord:
        """创建模型连接配置并返回含数据库主键的记录。"""

        record = ModelConfigRecord(
            config_name=self._required(config_name, "config_name"),
            base_url=self._required(base_url, "base_url").rstrip("/"),
            api_key=self._required(api_key, "api_key"),
            model_name=self._required(model_name, "model_name"),
            context_window_k=self._positive(context_window_k, "context_window_k"),
            supports_thinking=bool(supports_thinking),
            supports_reasoning_effort=bool(supports_reasoning_effort),
            supports_image=bool(supports_image),
            sort_order=sort_order,
        )
        with self._session_factory.begin() as session:
            row = record.to_model()
            session.add(row)
            session.flush()
            return ModelConfigRecord.from_model(row)

    def list_all(self, enabled: bool | None = None) -> list[ModelConfigRecord]:
        """按排序权重和创建时间列出配置。"""

        with self._session_factory() as session:
            statement = select(ModelConfigModel).order_by(
                asc(ModelConfigModel.sort_order),
                asc(ModelConfigModel.created_at),
                asc(ModelConfigModel.id),
            )
            if enabled is not None:
                statement = statement.where(ModelConfigModel.enabled == enabled)
            rows = session.execute(statement).scalars().all()
        return [ModelConfigRecord.from_model(row) for row in rows]

    def get(self, config_id: int) -> ModelConfigRecord:
        """读取单个模型配置，不存在时抛 ``KeyError``。"""

        with self._session_factory() as session:
            row = session.get(ModelConfigModel, config_id)
        if row is None:
            raise KeyError(config_id)
        return ModelConfigRecord.from_model(row)

    def update(self, config_id: int, **changes: object) -> ModelConfigRecord:
        """更新显式传入的配置字段并返回最新记录。"""

        values = dict(changes)
        for field in ("config_name", "base_url", "api_key", "model_name"):
            if field in values and values[field] is not None:
                values[field] = self._required(str(values[field]), field)
        if "base_url" in values and values["base_url"] is not None:
            values["base_url"] = str(values["base_url"]).rstrip("/")
        if "context_window_k" in values and values["context_window_k"] is not None:
            values["context_window_k"] = self._positive(
                int(values["context_window_k"]), "context_window_k"
            )
        with self._session_factory.begin() as session:
            result = session.execute(
                update(ModelConfigModel)
                .where(ModelConfigModel.id == config_id)
                .values(**values)
            )
        if not result.rowcount:
            raise KeyError(config_id)
        return self.get(config_id)

    def delete(self, config_id: int) -> None:
        """删除配置；历史 Run 通过外键规则保留自身快照。"""

        with self._session_factory.begin() as session:
            session.execute(delete(ModelConfigModel).where(ModelConfigModel.id == config_id))

    @staticmethod
    def _required(value: str, field: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field} must not be blank")
        return normalized

    @staticmethod
    def _positive(value: int, field: str) -> int:
        if value <= 0:
            raise ValueError(f"{field} must be greater than zero")
        return value
