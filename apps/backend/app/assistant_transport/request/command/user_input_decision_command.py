"""回传用户对「等待用户决定」请求的结构化决定。

本命令是 human-in-the-loop 的唯一决定入口：随续跑请求（同一批携带 ``runId``）到达后端，
在 wire 边界完成结构校验后映射为领域值对象 ``UserDecision``，再经执行器透传进 LangGraph 的
``Command(resume=...)`` 由 ``wait_user`` 节点消费。

决定**不落库**：它是一次请求的输入，不是持久事实。若进程在「用户提交」与「图消费」之间终止，
Run 仍停在等待态，用户重新提交即可；被批准工具自身的重复执行由它的条件状态迁移保证幂等。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.tools.schemas.user_decision import UserDecision, UserDecisionKind


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
    """一次续跑携带的决定集合。"""

    model_config = ConfigDict(extra="forbid")

    decisions: list[UserInputDecisionItem] = Field(min_length=1, max_length=32)

    @field_validator("decisions")
    @classmethod
    def validate_unique_request_ids(
        cls, value: list[UserInputDecisionItem]
    ) -> list[UserInputDecisionItem]:
        """拒绝同一请求的重复决定。

        同请求给两个决定时「哪个生效」没有业务含义，且会让后端与前端各自以为生效的是自己那条，
        因此在 wire 边界直接拒绝（领域层的重复决定语义是「以最后一次为准」，只用于重试同值）。
        """

        request_ids = [item.request_id for item in value]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("同一请求不能在一次提交里出现多个决定")
        return value


class UserInputDecisionCommand(BaseModel):
    """把用户决定绑定到某个等待中的 Run 并触发续跑。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["custom"]
    commandId: str = Field(min_length=1, max_length=128)
    name: Literal["user-input-decision"]
    payload: UserInputDecisionPayload


    def to_user_decisions(self) -> tuple[UserDecision, ...]:
        """把已校验的 wire 命令映射为领域决定序列。

        参数:
            command: 已通过 pydantic 校验的决定命令。

        返回:
            领域值对象序列，顺序与 wire 提交一致。

        异常:
            无（形状问题已在 wire 校验阶段拒绝）。

        副作用:
            无。
        """

        return tuple(
            UserDecision(
                request_id=item.request_id,
                kind=item.decision,
                data=dict(item.data),
            )
            for item in self.payload.decisions
        )


__all__ = [
    "UserInputDecisionCommand",
    "UserInputDecisionItem",
    "UserInputDecisionPayload",
]
