"""Pydantic arguments for the delegate_task tool.

本模型用三个字段（child_agent_id / title / prompt）描述父 Agent 指派给子 Agent 的
委派契约。任务内容通过自由文本 ``prompt`` 承载，父 Agent 不被迫拆成结构化字段；
任务契约结构（Objective / Rules / References / Expected Output）由 handler 的
description 模板软引导，而非此处强制。预算校验退化为 ``prompt`` 总长度硬校验与
``title`` 必填/长度校验，超限时返回明确中英文错误并通过规范日志事件记录超限字段
与实测长度，便于排查父 Agent 调用问题。
"""

from pydantic import BaseModel, Field, model_validator

from app.config.logging.logger import log

# 预算常量
PROMPT_MAX = 8000
TITLE_MAX = 20


class DelegateTaskArgs(BaseModel):
    """描述父 Agent 指派给子 Agent 的委派契约。

    面向子 Agent 的任务信息统一通过 ``prompt`` 自由文本承载，父 Agent 只需给出
    单一目标文本；``title`` 仅用于展示与可追溯，``child_agent_id`` 决定目标
    child Agent profile。预算校验在 ``model_validator(mode="after")`` 中统一进行，
    超限返回中英文错误信息。
    """

    child_agent_id: str = Field(
        description=(
            "Target child AgentProfile id. Use delegate_reviewer for review-only code review, "
            "delegate_analyst for read-only investigation or document/code analysis, and "
            "delegate_coder for scoped code changes plus verification."
        )
    )
    title: str = Field(
        description=(
            "Short title for the delegated task, used for display and traceability. "
            "It must not duplicate the prompt or carry secrets; it is required and "
            "must be a non-empty, non-whitespace string."
        ),
    )
    prompt: str = Field(
        description=(
            "The full task contract for the child agent as free-form text. Structure it "
            "with markdown sections: Objective (the single goal), Rules (hard constraints), "
            "References (relevant paths or documents), and Expected Output (what the child "
            "returns), plus an optional Background. This text is passed verbatim as the "
            "child turn's input."
        )
    )

    @model_validator(mode="after")
    def _validate_budget(self) -> "DelegateTaskArgs":
        """对全部字段执行预算校验，超限时抛出中英文错误并写日志。

        参数:
            无（基于实例已解析字段校验）。

        返回:
            校验通过的 ``DelegateTaskArgs`` 实例本身。

        异常:
            ValueError: 当 prompt 超过预算上限或 title 为空/超长时抛出，消息含
                英文错误键与面向模型的中文 reason。

        副作用:
            超限时通过 ``log.warning`` 以 ``delegate_task_args_over_budget`` 事件键记录
            超限字段、上限与实测长度（不记录任何 secret 或敏感信息）。
        """

        # title
        if not self.title or not self.title.strip():
            log.warning(
                "delegate_task_args_over_budget",
                extra={
                    "msg": "delegate_task title 为空或纯空白",
                    "data": {"field": "title"},
                },
            )
            raise ValueError(
                "delegate_task.title_required: 任务标题不能为空或纯空白，必须提供简洁的任务标题。"
            )
        if len(self.title) > TITLE_MAX:
            log.warning(
                "delegate_task_args_over_budget",
                extra={
                    "msg": "delegate_task title 超出预算上限",
                    "data": {"field": "title", "limit": TITLE_MAX, "actual": len(self.title)},
                },
            )
            raise ValueError(
                "delegate_task.title_over_budget: 任务标题不得超过 "
                f"{TITLE_MAX} 个字符（当前 {len(self.title)} 字符）。"
            )

        # prompt
        if not self.prompt or not self.prompt.strip():
            log.warning(
                "delegate_task_args_over_budget",
                extra={
                    "msg": "delegate_task prompt 为空或纯空白",
                    "data": {"field": "prompt"},
                },
            )
            raise ValueError(
                "delegate_task.prompt_required: 任务 prompt 不能为空或纯空白，"
                "必须提供委派任务内容。"
            )
        if len(self.prompt) > PROMPT_MAX:
            log.warning(
                "delegate_task_args_over_budget",
                extra={
                    "msg": "delegate_task prompt 超出预算上限",
                    "data": {"field": "prompt", "limit": PROMPT_MAX, "actual": len(self.prompt)},
                },
            )
            raise ValueError(
                "delegate_task.prompt_over_budget: 任务 prompt 不得超过 "
                f"{PROMPT_MAX} 个字符（当前 {len(self.prompt)} 字符）。"
            )

        return self
