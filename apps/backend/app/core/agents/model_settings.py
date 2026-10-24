"""Agent 的模型请求偏好值对象。

``ModelSettings`` 只表达「用户在 Agent 配置里明确指定的请求偏好」（采样参数、推理强度等），
值为 ``None`` 表示交给运行时默认值。模型连接与能力（Base URL、API Key、模型名、上下文窗口、
各项 supports_*）不属于本值对象：它们只来自受控的模型配置 service，在 Run 装配时由
``resolve_model`` 与偏好一起解析成 ``ResolvedModel``。因此 Agent JSON 即使写入连接字段也会按
未知字段拒绝，API Key 不可能经 Agent 文件伪造或落盘。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from typing import Any, Literal, get_args, get_origin, get_type_hints

from app.models.conversation_run_options import ReasoningEffort


def _preference_fields() -> tuple[str, ...]:
    return tuple(item.name for item in fields(ModelSettings))


def _accepted_value_types() -> dict[str, tuple[type, ...]]:
    """根据字段注解返回偏好项的 JSON 类型契约。"""

    accepted: dict[str, tuple[type, ...]] = {}
    type_hints = get_type_hints(ModelSettings)
    for name in _preference_fields():
        annotation = type_hints[name]
        members = tuple(member for member in get_args(annotation) if member is not type(None))
        json_types: list[type] = []
        for member in members or (annotation,):
            if get_origin(member) is Literal:
                json_types.extend(dict.fromkeys(type(value) for value in get_args(member)))
            else:
                json_types.append(member)
        accepted[name] = tuple(json_types)
    return accepted


def _accepts_value(value: Any, accepted: tuple[type, ...]) -> bool:
    """判断一个 JSON 值是否符合字段类型，避免把 bool 当作 int。"""

    if isinstance(value, bool):
        return bool in accepted
    if isinstance(value, int) and float in accepted and int not in accepted:
        return True
    return isinstance(value, accepted)


class ModelSettingsError(ValueError):
    """模型偏好项或模型配置不满足值对象契约。"""


@dataclass
class ModelSettings:
    """Agent 的模型请求偏好；全部字段可空，``None`` 表示不指定。"""

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    drop_params: bool | None = None
    stream: bool | None = None
    reasoning_effort: ReasoningEffort | None = None

    def __post_init__(self) -> None:
        """配置文件和直接构造都遵守 Run 支持的推理强度，错误在进入执行前暴露。"""
        if self.reasoning_effort is not None and self.reasoning_effort not in get_args(
            ReasoningEffort
        ):
            raise ModelSettingsError("reasoning_effort 必须是 low/high/max")

    @classmethod
    def default_settings(cls) -> ModelSettings:
        """返回系统默认的偏好项。"""

        return cls(stream=True, reasoning_effort="high")

    def with_defaults(self, fallback: ModelSettings | None) -> ModelSettings:
        """返回新对象：本对象未设置（``None``）的偏好项由 ``fallback`` 补齐，已设置的不被覆盖。"""

        if fallback is None:
            return replace(self)
        return replace(
            self,
            **{
                name: getattr(fallback, name)
                for name in _preference_fields()
                if getattr(self, name) is None and getattr(fallback, name) is not None
            },
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化非空偏好项。"""
        return {
            name: getattr(self, name)
            for name in _preference_fields()
            if getattr(self, name) is not None
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ModelSettings:
        """从内部字典构造偏好项，忽略非偏好字段（如旧快照里残留的连接字段）。"""

        return cls(**{name: data[name] for name in _preference_fields() if name in data})

    @classmethod
    def from_json(cls, values: Mapping[str, Any] | None) -> ModelSettings:
        """严格校验用户 JSON 偏好项并构造设置；连接字段按未知字段拒绝。"""

        if values is None or len(values) == 0:
            return cls.default_settings()
        accepted_types = _accepted_value_types()
        unknown = sorted(set(values) - set(accepted_types))
        if unknown:
            raise ModelSettingsError(f"model_settings 含未知字段: {', '.join(unknown)}")
        for name, value in values.items():
            accepted = accepted_types[name]
            if not _accepts_value(value, accepted):
                expected = "/".join(item.__name__ for item in accepted)
                raise ModelSettingsError(f"model_settings.{name} 必须是 {expected}")
            if isinstance(value, float) and not math.isfinite(value):
                raise ModelSettingsError(f"model_settings.{name} 必须是有限数值")
        try:
            return cls(**values)
        except (TypeError, ValueError) as exc:
            raise ModelSettingsError(f"model_settings 构造失败: {exc}") from exc
