from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.assistant_transport.request import TransportRequestError


class ForkTaskRequest(BaseModel):
    """校验历史 Run fork 请求。"""

    model_config = ConfigDict(populate_by_name=True)

    run_id: int = Field(alias="runId")

    @model_validator(mode="before")
    @classmethod
    def validate_wire(cls, value: object) -> object:
        """将 Fork 请求的纯 wire 校验转换为项目统一的结构化异常。"""

        if not isinstance(value, dict) or "runId" not in value:
            raise TransportRequestError(
                status_code=422,
                code="FORK_RUN_ID_REQUIRED",
                message="fork 请求必须提供正整数 runId",
                retryable=False,
            )
        run_id = value["runId"]
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
            raise TransportRequestError(
                status_code=422,
                code="FORK_RUN_ID_INVALID",
                message="fork 请求的 runId 必须是正整数",
                retryable=False,
            )
        return value
