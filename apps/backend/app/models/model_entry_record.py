"""模型条目持久化状态值对象。

单一职责：承载厂商下一个模型条目（ModelEntry）的不可变业务数据、字段校验与
序列化（to_dict）。不负责数据库操作（由 ``storage/crud/model_entry_crud`` 负责）。

字段校验经 Pydantic ``field_validator`` 收口在本值对象内（模型自身不变量）：
``model_name`` / ``display_name`` 去空白后不能为空，``max_context_window``
必须为正整数；CRUD 组装记录时直接构造，非法输入由校验器拒绝。

``LLMRuntimeConfig.from_model_entry`` 从本值对象（连同 ProviderRecord）构造
运行时配置（见 ``docs/agent-model-provider-design.md`` §6.1），DB 行是采样
参数 / thinking / 窗口的唯一事实源（D16）。
"""

from datetime import datetime

from pydantic import field_validator
from pydantic.dataclasses import dataclass

from app.storage.model.model_entry_model import ModelEntryModel
from app.utils.datetime_utils import from_text, to_text


def _normalize_required_text(value: object) -> str:
    """去空白并拒绝空串（模型名 / 展示名不可为空）。

    供 ``ModelEntryRecord`` 校验器与 ``ModelEntryCrud.update`` 复用，是
    「必填文本字段」归一规则的单一实现。

    参数:
        value: 原始字段值（可能非 str，统一经 str() 归一）。

    返回:
        去首尾空白后的非空字符串。

    异常:
        ValueError: 归一后为空时抛出。
    """
    normalized = str(value).strip()
    if not normalized:
        raise ValueError("must not be blank")
    return normalized


def _positive_context_window(value: object) -> int:
    """把上下文窗口归一为 int 并拒绝非正值。

    供 ``ModelEntryRecord`` 校验器与 ``ModelEntryCrud.update`` 复用，是
    「窗口必须为正」规则的单一实现。

    参数:
        value: 原始窗口值（可能为数字字符串）。

    返回:
        正整数的上下文窗口 token 数。

    异常:
        ValueError: 非正整数时抛出。
    """
    window = int(str(value))
    if window <= 0:
        raise ValueError("must be positive")
    return window


@dataclass
class ModelEntryRecord:
    """表示某厂商下的一个可用模型条目。"""

    model_id: str
    provider_id: str
    model_name: str
    display_name: str
    max_context_window: int
    created_at: datetime
    updated_at: datetime
    supports_thinking: bool = False
    supports_image: bool = False
    supports_video: bool = False
    enabled: bool = True
    sort_order: int = 0

    @field_validator("model_name", "display_name", mode="before")
    @classmethod
    def _validate_required_text(cls, value: object) -> str:
        """委托 :func:`_normalize_required_text` 做必填文本校验。"""
        return _normalize_required_text(value)

    @field_validator("max_context_window", mode="before")
    @classmethod
    def _validate_positive_window(cls, value: object) -> int:
        """委托 :func:`_positive_context_window` 做窗口校验。"""
        return _positive_context_window(value)

    def to_dict(self) -> dict[str, str | int | float | bool | None]:
        """将模型条目状态转换为可序列化为 JSON 的字典。

        参数:
            无。

        返回:
            包含模型条目字段的字典。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "model_name": self.model_name,
            "display_name": self.display_name,
            "max_context_window": self.max_context_window,
            "supports_thinking": self.supports_thinking,
            "supports_image": self.supports_image,
            "supports_video": self.supports_video,
            "enabled": self.enabled,
            "sort_order": self.sort_order,
            "created_at": to_text(self.created_at),
            "updated_at": to_text(self.updated_at),
        }

    @classmethod
    def from_model(cls, row: ModelEntryModel) -> "ModelEntryRecord":
        """从 ORM 行构造模型条目记录值对象。

        参数:
            row: ``models`` 表的 SQLAlchemy 行对象。

        返回:
            对应的 ``ModelEntryRecord``；文本时间戳经 ``from_text`` 还原为 datetime。

        异常:
            pydantic.ValidationError: 如果 DB 行存在脏数据（如空 model_name /
                display_name 或非正 max_context_window），经本值对象校验器拒绝时抛出。

        副作用:
            无。
        """
        return cls(
            model_id=row.model_id,
            provider_id=row.provider_id,
            model_name=row.model_name,
            display_name=row.display_name,
            max_context_window=row.max_context_window,
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
            supports_thinking=bool(row.supports_thinking),
            supports_image=bool(row.supports_image),
            supports_video=bool(row.supports_video),
            enabled=bool(row.enabled),
            sort_order=row.sort_order,
        )

    def to_model(self) -> ModelEntryModel:
        """将模型条目记录值对象转换为 ORM 行对象。

        参数:
            无。

        返回:
            对应的 ``models`` 表的 SQLAlchemy 行对象。

        异常:
            无。

        副作用:
            无。
        """
        return ModelEntryModel(
            model_id=self.model_id,
            provider_id=self.provider_id,
            model_name=self.model_name,
            display_name=self.display_name,
            max_context_window=self.max_context_window,
            supports_thinking=self.supports_thinking,
            enabled=self.enabled,
            sort_order=self.sort_order,
            supports_image=self.supports_image,
            supports_video=self.supports_video,
            created_at=to_text(self.created_at),
            updated_at=to_text(self.updated_at),
        )
