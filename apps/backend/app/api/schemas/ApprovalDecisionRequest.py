
from pydantic import BaseModel, field_validator

class ApprovalDecisionRequest(BaseModel):
    """校验审批决策请求体。

    参数:
        decision: 审批决策，必须是 approved 或 denied。
        reason: 可选的人类可读原因。
        idempotency_key: 前端生成的幂等键。

    返回:
        Pydantic 请求模型。

    异常:
        ValueError: 当 decision 或 idempotency_key 非法时抛出。

    副作用:
        无。
    """

    decision: str
    reason: str = None
    idempotency_key: str

    @field_validator("decision")
    @classmethod
    def decision_must_be_supported(cls, value: str) -> str:
        """校验审批决策值。

        参数:
            value: 从请求体解析出的审批决策。

        返回:
            校验通过的审批决策。

        异常:
            ValueError: 如果决策不是 approved 或 denied。

        副作用:
            无。
        """

        if value not in {"approved", "denied"}:
            raise ValueError("decision must be approved or denied")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def idempotency_key_must_not_be_blank(cls, value: str) -> str:
        """校验幂等键不为空。

        参数:
            value: 从请求体解析出的幂等键。

        返回:
            校验通过的幂等键。

        异常:
            ValueError: 如果幂等键为空白。

        副作用:
            无。
        """

        if not value.strip():
            raise ValueError("idempotency_key must not be blank")
        return value