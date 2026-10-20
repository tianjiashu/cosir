"""等待用户的 wire 命令：结构校验后映射 UserDecision，由命令服务与 Run 状态原子持久化。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.user_decision import UserDecision, UserDecisionKind


class UserInputDecisionItem(BaseModel):
    """单条用户决定（wire 形状）。

    ``decision`` 直接复用领域枚举，避免 wire 值与领域值各维护一份导致漂移；``data`` 是用户
    编辑后的结构化输入，形状由提问工具定义（后端在此不做业务校验）。
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128)
    decision: UserDecisionKind
    data: dict[str, Any] = Field(default_factory=dict)


class UserInputDecisionPayload(BaseModel):
    """一次续跑携带的单个用户决定。"""

    model_config = ConfigDict(extra="forbid")

    decision: UserInputDecisionItem


class UserInputDecisionCommand(BaseModel):
    """把用户决定绑定到某个等待中的 Run 并触发续跑。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["custom"]
    commandId: str = Field(min_length=1, max_length=128)
    name: Literal["user-input-decision"]
    payload: UserInputDecisionPayload

    def to_user_decision(self) -> UserDecision:
        """把已校验的 wire 命令映射为领域决定。

        参数:
            command: 已通过 pydantic 校验的决定命令。

        返回:
            对应的领域值对象。

        异常:
            无（形状问题已在 wire 校验阶段拒绝）。

        副作用:
            无。
        """

        item = self.payload.decision
        return UserDecision(
            request_id=item.request_id,
            kind=item.decision,
            data=dict(item.data),
        )


__all__ = [
    "UserInputDecisionCommand",
    "UserInputDecisionItem",
    "UserInputDecisionPayload",
]
