"""Agent 模型运行配置值对象。

``ModelSettings`` 同时承载两类不同来源、但在模型构建时必须完整可用的事实：

* 用户可覆盖的请求参数，例如采样参数和推理强度；
* 已从 ``ModelConfigRecord`` 物化的模型连接与能力字段。

模型配置 ID 不属于本值对象。它只在配置编辑和 Run 选择边界存在，进入运行时后立即
转换为这里的字段，保证模型构建不需要再次回查配置服务。用户配置 JSON 只允许写入
覆盖项，连接密钥等运行时字段不会被序列化或接受为用户输入。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from typing import TYPE_CHECKING, Any, get_args, get_type_hints

if TYPE_CHECKING:
    from app.models.model_config_record import ModelConfigRecord


# 这些字段来自模型连接配置，是运行时构建模型的完整输入，不属于用户 JSON 覆盖项。
_RUNTIME_FIELDS = (
    "base_url",
    "api_key",
    "model_name",
    "context_window_k",
    "supports_thinking",
    "supports_reasoning_effort",
    "supports_image",
)


def _override_fields() -> tuple[str, ...]:
    """返回可由 Agent JSON 配置覆盖的字段清单。"""

    runtime_fields = set(_RUNTIME_FIELDS)
    return tuple(field.name for field in fields(ModelSettings) if field.name not in runtime_fields)


def _accepted_value_types() -> dict[str, tuple[type, ...]]:
    """根据字段注解返回用户覆盖项的 JSON 类型契约。"""

    accepted: dict[str, tuple[type, ...]] = {}
    type_hints = get_type_hints(ModelSettings)
    for name in _override_fields():
        annotation = type_hints[name]
        members = tuple(member for member in get_args(annotation) if member is not type(None))
        accepted[name] = members or (annotation,)
    return accepted


def _accepts_value(value: Any, accepted: tuple[type, ...]) -> bool:
    """判断一个 JSON 值是否符合字段类型，避免把 bool 当作 int。"""

    if isinstance(value, bool):
        return bool in accepted
    if isinstance(value, int) and float in accepted and int not in accepted:
        return True
    return isinstance(value, accepted)


class ModelSettingsError(ValueError):
    """表示用户模型覆盖项或运行时模型配置不满足值对象契约。"""


@dataclass
class ModelSettings:
    """构建一次聊天模型所需的完整运行配置。

    用户覆盖字段只表达“用户明确指定的请求偏好”，值为 ``None`` 时交给统一运行时
    默认值处理。运行时字段由模型连接配置物化而来，供 ``resolve_chat_model`` 唯一消费；
    其中 ``api_key`` 仅存在于进程内，不参与序列化和日志输出。
    """

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    drop_params: bool | None = None
    stream: bool | None = None
    reasoning_effort: str | None = None

    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    model_name: str | None = None
    context_window_k: int | None = None
    supports_thinking: bool | None = None
    supports_reasoning_effort: bool | None = None
    supports_image: bool | None = None

    @classmethod
    def default_settings(cls) -> ModelSettings:
        """返回系统默认的用户覆盖项。"""

        return cls(stream=True, reasoning_effort="high")

    @classmethod
    def from_model_config_record(cls, config: ModelConfigRecord) -> ModelSettings:
        """把模型连接配置物化为不含模型配置 ID 的运行设置。

        该转换只写入连接字段和能力字段，不隐式写入 Agent/Run 的运行偏好。
        """

        if not config.model_name or config.context_window_k <= 0:
            raise ModelSettingsError("模型连接配置缺少有效的 model_name 或 context_window_k")
        return cls(
            base_url=config.base_url,
            api_key=config.api_key,
            model_name=config.model_name,
            context_window_k=config.context_window_k,
            supports_thinking=config.supports_thinking,
            supports_reasoning_effort=config.supports_reasoning_effort,
            supports_image=config.supports_image,
        )

    def with_overrides(self, overrides: ModelSettings | None) -> ModelSettings:
        """将另一个设置中的用户覆盖项合并到当前运行设置。

        运行时模型字段始终由当前对象保留，避免调用方通过一个未物化的覆盖对象清空
        连接能力或密钥。该方法返回新对象，不修改任一输入。
        """

        if overrides is None:
            return replace(self)
        changes = {
            name: getattr(overrides, name)
            for name in _override_fields()
            if getattr(overrides, name) is not None
        }
        return replace(self, **changes)

    def with_preference_defaults(self, fallback: ModelSettings | None) -> ModelSettings:
        """用另一份设置补齐本对象缺失的用户覆盖项。

        与 :meth:`with_overrides` 的区别：本对象（通常是本次 Run 物化出的运行配置）的
        运行时字段与已设置的覆盖项全部保留，``fallback`` 只在对应覆盖项为 ``None`` 时
        提供默认值，**不会覆盖**本对象已有的偏好。运行时字段不参与补齐，因此不会用
        一份未物化对象的连接信息顶替本对象的连接配置。

        参数:
            fallback: 偏好默认值来源（通常是 Agent profile 的用户覆盖项）；``None``
                表示没有默认值可补，返回本对象的等值副本。

        返回:
            补齐后的新对象；``self`` 与 ``fallback`` 均不被修改。
        """

        if fallback is None:
            return replace(self)
        changes = {
            name: getattr(fallback, name)
            for name in _override_fields()
            if getattr(self, name) is None and getattr(fallback, name) is not None
        }
        if not changes:
            return replace(self)
        return replace(self, **changes)

    def require_runtime_config(self) -> ModelSettings:
        """断言当前设置已具备模型构建所需的物化字段。"""

        required = (
            "model_name",
            "base_url",
            "context_window_k",
            "supports_thinking",
            "supports_reasoning_effort",
            "supports_image",
        )
        missing = [name for name in required if getattr(self, name) is None]
        if missing:
            raise ModelSettingsError(
                "模型运行配置未物化，缺少字段: " + ", ".join(missing)
            )
        if self.context_window_k <= 0:
            raise ModelSettingsError("模型运行配置的 context_window_k 必须为正整数")
        return self

    def to_dict(self) -> dict[str, Any]:
        """序列化用户可持久化的非空覆盖项，不输出运行时字段和 API Key。"""

        return {
            name: getattr(self, name)
            for name in _override_fields()
            if getattr(self, name) is not None
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ModelSettings:
        """从内部字典构造用户覆盖项，忽略非覆盖字段。"""

        return cls(**{name: data[name] for name in _override_fields() if name in data})

    @classmethod
    def from_json(cls, values: Mapping[str, Any] | None) -> ModelSettings:
        """严格校验用户 JSON 覆盖项并构造设置。

        运行时连接字段即使出现在 JSON 中也会按未知字段拒绝；这保证 API Key、能力声明
        和模型连接地址只能来自受控的模型配置 service，而不是 Agent 文件自行伪造。
        """

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
