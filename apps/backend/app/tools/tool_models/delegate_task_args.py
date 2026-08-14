"""Pydantic arguments for the delegate_task tool.

本模型用结构化字段（objective / rules / references / expected_output）描述父 Agent
指派给子 Agent 的任务契约，替代旧版自由文本 ``prompt`` 与 ``requested_tools``。
同时统一校验各字段预算（字符数与项数上限），超限时返回明确中英文错误并通过
规范日志事件记录超限字段与实测长度，便于排查父 Agent 调用问题。
"""

from pydantic import BaseModel, Field, model_validator

from app.config.logging.logger import log

# 预算常量（与计划 Task 2 一致）
OBJECTIVE_MAX = 2000
TITLE_MAX = 20
LIST_MAX_ITEMS = 10
LIST_ITEM_MAX = 500
LIST_TOTAL_MAX = 2000
EXPECTED_OUTPUT_MAX = 2000
BACKGROUND_MAX = 2000


class DelegateTaskArgs(BaseModel):
    """描述父 Agent 指派给子 Agent 的结构化任务契约。

    所有面向子 Agent 的任务信息均通过结构化字段表达，父 Agent 不再自由拼装 prompt。
    校验在 model_validator(mode="after") 中统一进行，超限返回中英文错误信息。
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
            "It must not duplicate the objective or carry secrets; it is required and "
            "must be a non-empty, non-whitespace string."
        ),
    )
    objective: str = Field(
        description=(
            "The single, focused goal the child agent must achieve. State what success looks "
            "like without unrelated conversation history."
        )
    )
    rules: list[str] = Field(
        description=(
            "Hard constraints the child must follow, e.g. do not modify files outside the "
            "given scope, do not run destructive commands. Each item is one rule. "
            "MAXIMUM 10 items — if you have more, merge or prioritize; exceeding 10 causes "
            "immediate validation failure and the call will be rejected."
        )
    )
    references: list[str] = Field(
        description=(
            "Background or reference paths the child should consult: file paths, module names, "
            "or documents relevant to the task. MAXIMUM 10 items — pick only the most relevant; "
            "exceeding 10 causes immediate validation failure and the call will be rejected."
        )
    )
    expected_output: str = Field(
        description=(
            "What the child must return on completion: the format, artifacts, and confirmation "
            "expected from the child agent."
        )
    )
    background: str = Field(
        default="",
        description=(
            "Optional free-form context the parent agent adds to supplement the task: "
            "clarifications, assumptions, environment notes, or anything not fitting "
            "objective/rules/references/expected_output. Empty by default; when provided it "
            "is included as a Background section."
        ),
    )

    @model_validator(mode="after")
    def _validate_budget(self) -> "DelegateTaskArgs":
        """对全部字段执行预算校验，超限时抛出中英文错误并写日志。

        参数:
            无（基于实例已解析字段校验）。

        返回:
            校验通过的 ``DelegateTaskArgs`` 实例本身。

        异常:
            ValueError: 当任一字段超过预算上限（字符数或项数）时抛出，消息含
                英文错误键与面向模型的中文 reason。

        副作用:
            超限时通过 ``log.warning`` 以 ``delegate_task_args_over_budget`` 事件键记录
            超限字段、上限与实测长度（不记录任何 secret 或敏感信息）。
        """

        # objective
        if len(self.objective) > OBJECTIVE_MAX:
            log.warning(
                "delegate_task_args_over_budget",
                extra={
                    "msg": "delegate_task objective 超出预算上限",
                    "data": {
                        "field": "objective",
                        "limit": OBJECTIVE_MAX,
                        "actual": len(self.objective),
                    },
                },
            )
            raise ValueError(
                "delegate_task.objective_over_budget: 任务目标不得超过 "
                f"{OBJECTIVE_MAX} 个字符（当前 {len(self.objective)} 字符）。"
            )

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

        # rules
        if len(self.rules) > LIST_MAX_ITEMS:
            log.warning(
                "delegate_task_args_over_budget",
                extra={
                    "msg": "delegate_task rules 项数超出预算上限",
                    "data": {
                        "field": "rules",
                        "limit": LIST_MAX_ITEMS,
                        "actual": len(self.rules),
                    },
                },
            )
            raise ValueError(
                "delegate_task.rules_too_many: 约束规则不得超过 "
                f"{LIST_MAX_ITEMS} 项（当前 {len(self.rules)} 项）。"
            )
        rules_total = sum(len(item) for item in self.rules)
        for item in self.rules:
            if len(item) > LIST_ITEM_MAX:
                log.warning(
                    "delegate_task_args_over_budget",
                    extra={
                        "msg": "delegate_task rules 单项超出预算上限",
                        "data": {
                            "field": "rules.item",
                            "limit": LIST_ITEM_MAX,
                            "actual": len(item),
                        },
                    },
                )
                raise ValueError(
                    "delegate_task.rules_item_over_budget: 每条约束规则不得超过 "
                    f"{LIST_ITEM_MAX} 个字符（当前 {len(item)} 字符）。"
                )
        if rules_total > LIST_TOTAL_MAX:
            log.warning(
                "delegate_task_args_over_budget",
                extra={
                    "msg": "delegate_task rules 合计超出预算上限",
                    "data": {
                        "field": "rules.total",
                        "limit": LIST_TOTAL_MAX,
                        "actual": rules_total,
                    },
                },
            )
            raise ValueError(
                "delegate_task.rules_total_over_budget: 约束规则合计不得超过 "
                f"{LIST_TOTAL_MAX} 个字符（当前 {rules_total} 字符）。"
            )

        # references
        if len(self.references) > LIST_MAX_ITEMS:
            log.warning(
                "delegate_task_args_over_budget",
                extra={
                    "msg": "delegate_task references 项数超出预算上限",
                    "data": {
                        "field": "references",
                        "limit": LIST_MAX_ITEMS,
                        "actual": len(self.references),
                    },
                },
            )
            raise ValueError(
                "delegate_task.references_too_many: 参考条目不得超过 "
                f"{LIST_MAX_ITEMS} 项（当前 {len(self.references)} 项）。"
            )
        references_total = sum(len(item) for item in self.references)
        for item in self.references:
            if len(item) > LIST_ITEM_MAX:
                log.warning(
                    "delegate_task_args_over_budget",
                    extra={
                        "msg": "delegate_task references 单项超出预算上限",
                        "data": {
                            "field": "references.item",
                            "limit": LIST_ITEM_MAX,
                            "actual": len(item),
                        },
                    },
                )
                raise ValueError(
                    "delegate_task.references_item_over_budget: 每条参考不得超过 "
                    f"{LIST_ITEM_MAX} 个字符（当前 {len(item)} 字符）。"
                )
        if references_total > LIST_TOTAL_MAX:
            log.warning(
                "delegate_task_args_over_budget",
                extra={
                    "msg": "delegate_task references 合计超出预算上限",
                    "data": {
                        "field": "references.total",
                        "limit": LIST_TOTAL_MAX,
                        "actual": references_total,
                    },
                },
            )
            raise ValueError(
                "delegate_task.references_total_over_budget: 参考条目合计不得超过 "
                f"{LIST_TOTAL_MAX} 个字符（当前 {references_total} 字符）。"
            )

        # expected_output
        if len(self.expected_output) > EXPECTED_OUTPUT_MAX:
            log.warning(
                "delegate_task_args_over_budget",
                extra={
                    "msg": "delegate_task expected_output 超出预算上限",
                    "data": {
                        "field": "expected_output",
                        "limit": EXPECTED_OUTPUT_MAX,
                        "actual": len(self.expected_output),
                    },
                },
            )
            raise ValueError(
                "delegate_task.expected_output_over_budget: 期望产出不得超过 "
                f"{EXPECTED_OUTPUT_MAX} 个字符（当前 {len(self.expected_output)} 字符）。"
            )

        # background（可选补充，空时跳过）
        if self.background and len(self.background) > BACKGROUND_MAX:
            log.warning(
                "delegate_task_args_over_budget",
                extra={
                    "msg": "delegate_task background 超出预算上限",
                    "data": {
                        "field": "background",
                        "limit": BACKGROUND_MAX,
                        "actual": len(self.background),
                    },
                },
            )
            raise ValueError(
                "delegate_task.background_over_budget: 补充背景不得超过 "
                f"{BACKGROUND_MAX} 个字符（当前 {len(self.background)} 字符）。"
            )

        return self
