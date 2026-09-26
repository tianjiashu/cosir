"""delegate_task 的结构参数与长度预算校验。"""

from pydantic import BaseModel, Field, model_validator

from app.config.logging.logger import log

MESSAGE_MAX = 3000
AGENT_NAME_MAX = 10


class DelegateTaskArgs(BaseModel):
    """描述父 Agent 指派给子 Agent 的委派契约。

    子 Agent ID 的候选提示由每个 Run 的 workspace profile 目录投影到 JSON Schema；
    本模型只负责字段结构和文本预算，不读取进程级目录，也不承担委派授权裁决。
    """

    child_agent_id: str = Field(
        description="Target child Agent id. REQUIRED. Choose one of the listed available agents."
    )
    agent_name: str = Field(
        description=(
            "Short noun-phrase label naming the agent; prefer a unique name. "
            f"REQUIRED, at most {AGENT_NAME_MAX} characters."
        )
    )
    message: str = Field(
        description=(
            "The complete task contract as free-form text, passed verbatim as the child's "
            f"entire input. REQUIRED, at most {MESSAGE_MAX} characters."
        )
    )

    @model_validator(mode="after")
    def _validate_budget(self) -> "DelegateTaskArgs":
        """校验委派标题和任务正文的必填条件及长度预算。

        返回:
            校验通过的参数对象。

        异常:
            ValueError: 标题或正文为空、纯空白或超过长度上限。

        副作用:
            拒绝超限参数时写入不含正文内容的结构化 warning 日志。
        """

        if not self.agent_name or not self.agent_name.strip():
            log.warning(
                "delegate_task_args_over_budget",
                extra={
                    "msg": "delegate_task agent_name 为空或纯空白",
                    "data": {"field": "agent_name"},
                },
            )
            raise ValueError("delegate_task.agent_name_required: 任务标题不能为空或纯空白。")
        if len(self.agent_name) > AGENT_NAME_MAX:
            log.warning(
                "delegate_task_args_over_budget",
                extra={
                    "msg": "delegate_task agent_name 超出预算上限",
                    "data": {
                        "field": "agent_name",
                        "limit": AGENT_NAME_MAX,
                        "actual": len(self.agent_name),
                    },
                },
            )
            raise ValueError(
                f"delegate_task.agent_name_over_budget: 任务标题不得超过 {AGENT_NAME_MAX} 个字符。"
            )
        if not self.message or not self.message.strip():
            log.warning(
                "delegate_task_args_over_budget",
                extra={"msg": "delegate_task message 为空或纯空白", "data": {"field": "message"}},
            )
            raise ValueError("delegate_task.message_required: 委派任务内容不能为空或纯空白。")
        if len(self.message) > MESSAGE_MAX:
            log.warning(
                "delegate_task_args_over_budget",
                extra={
                    "msg": "delegate_task message 超出预算上限",
                    "data": {"field": "message", "limit": MESSAGE_MAX, "actual": len(self.message)},
                },
            )
            raise ValueError(
                f"delegate_task.message_over_budget: 任务 message 不得超过 {MESSAGE_MAX} 个字符。"
            )
        return self
