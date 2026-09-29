"""Conversation Run 的模型能力与最终运行偏好快照。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ReasoningEffort = Literal["low", "high", "max"]


@dataclass(frozen=True, slots=True)
class ConversationRunModelSnapshot:
    """保存一次 Run 实际使用的模型能力声明和运行偏好。

    模型能力从 ``model_configs`` 复制而来，运行偏好由 Agent 默认配置与本次请求覆盖合并
    得到。``reasoning_effort`` 为本次 Run 的显式覆盖；为空时由 Agent 运行偏好提供最终值。
    ``supports_thinking`` 只表示模型能力，不是用户可关闭的运行开关。该对象只承载持久化事实，
    不负责读取数据库、构建模型或执行图片处理。

    参数:
        supports_thinking: 创建本次 Run 时用户声明模型支持思考输出。
        supports_reasoning_effort: 创建本次 Run 时用户声明模型支持统一推理强度。
        supports_image: 创建本次 Run 时用户声明模型支持图片输入。
        reasoning_effort: 本次 Run 的统一推理强度覆盖；``None`` 表示沿用 Agent 偏好。

    异常:
        TypeError: 字段类型不符合持久化契约。
        ValueError: 推理强度不在统一允许集合内，或启用了模型不支持的能力。
    """

    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_image: bool
    reasoning_effort: ReasoningEffort | None

    def __post_init__(self) -> None:
        """校验快照的一致性，阻止无效能力与运行偏好进入 ``Run.extra``。"""

        if not all(
            isinstance(value, bool)
            for value in (
                self.supports_thinking,
                self.supports_reasoning_effort,
                self.supports_image,
            )
        ):
            raise TypeError("model snapshot capability fields must be boolean")
        if self.reasoning_effort not in (None, "low", "high", "max"):
            raise ValueError("reasoning_effort must be one of low/high/max")
        if self.reasoning_effort is not None and not self.supports_reasoning_effort:
            raise ValueError(
                "reasoning_effort cannot be set when the model does not support it"
            )

    def to_dict(self) -> dict[str, object]:
        """转换为可写入 ``conversation_runs.extra`` 的 JSON 对象。"""

        return {
            "supports_thinking": self.supports_thinking,
            "supports_reasoning_effort": self.supports_reasoning_effort,
            "supports_image": self.supports_image,
            "reasoning_effort": self.reasoning_effort,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ConversationRunModelSnapshot":
        """从当前快照结构恢复值对象；结构不正确时直接抛出异常。"""

        if not isinstance(value, dict):
            raise TypeError("model_snapshot must be an object")
        expected = {
            "supports_thinking",
            "supports_reasoning_effort",
            "supports_image",
            "reasoning_effort",
        }
        unknown = set(value) - expected
        missing = expected - set(value)
        if unknown or missing:
            raise ValueError(
                f"model_snapshot fields invalid; unknown={sorted(unknown)}, missing={sorted(missing)}"
            )
        return cls(
            supports_thinking=value["supports_thinking"],
            supports_reasoning_effort=value["supports_reasoning_effort"],
            supports_image=value["supports_image"],
            reasoning_effort=value["reasoning_effort"],
        )


__all__ = ["ConversationRunModelSnapshot", "ReasoningEffort"]
