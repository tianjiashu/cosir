"""delegate_task 的结构参数与长度预算校验。"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config.logging.logger import log

MESSAGE_MAX = 3000
AGENT_NAME_MAX = 10


class DelegateTaskArgs(BaseModel):
    """描述父 Agent 指派给子 Agent 的委派契约。

    子 Agent ID 的候选提示由每个 Run 的 workspace profile 目录投影到 JSON Schema；
    本模型只负责字段结构和文本预算，不读取进程级目录，也不承担委派授权裁决。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    child_agent_id: str = Field(
        description=(
            "The child agent to run the task, from the available child agents listed in your "
            "instructions: pick the one whose description and allowed_tools fit the task. REQUIRED."
        )
    )
    agent_name: str = Field(
        description=(
            "Short name for this child, shown to the user as its conversation title and on its "
            "status line. Name it after its job (e.g. 'API review') so parallel children can be "
            f"told apart. REQUIRED, at most {AGENT_NAME_MAX} characters."
        )
    )
    # 不写「越详细越好」：它与长度上限冲突，还诱导模型把文件内容贴进来；按要素写、文件按路径指。
    message: str = Field(
        description=(
            "The brief, passed verbatim as the child's entire input; it is all the child knows "
            "about this task. State the goal, what done looks like and what to report back, what "
            "you have already found or ruled out, and the scope. Point it at files and docs by "
            "path instead of pasting their contents. "
            f"REQUIRED, at most {MESSAGE_MAX} characters."
        )
    )

    writable_root: str | None = Field(
        default=None,
        description=(
            "Optional. An existing directory inside the workspace, relative to the workspace root, "
            "that the child may write to: its file-writing tools are refused elsewhere and its "
            "commands start there. It can only narrow your own writable area, never widen it. "
            "Give parallel children separate directories so they cannot overwrite each other. "
            "Omit to give the child the same writable area as you."
        ),
    )

    @model_validator(mode="after")
    def _validate_budget(self) -> "DelegateTaskArgs":
        """校验委派标题和会话正文的必填条件及长度预算。

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
            raise ValueError("delegate_task.agent_name_required: 会话标题不能为空或纯空白。")
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
                f"delegate_task.agent_name_over_budget: 会话标题不得超过 {AGENT_NAME_MAX} 个字符。"
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
                f"delegate_task.message_over_budget: 会话 message 不得超过 {MESSAGE_MAX} 个字符。"
            )
        return self
