"""Pydantic arguments for the delegate_task tool.

本模型用三个字段（child_agent_id / title / prompt）描述父 Agent 指派给子 Agent 的
委派契约。任务内容通过自由文本 ``prompt`` 承载，父 Agent 不被迫拆成结构化字段；
任务契约结构（Objective / Rules / References / Expected Output）由 handler 的
description 模板软引导，而非此处强制。预算校验退化为 ``prompt`` 总长度硬校验与
``title`` 必填/长度校验，超限时返回明确中英文错误并通过规范日志事件记录超限字段
与实测长度，便于排查父 Agent 调用问题。
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from pydantic.json_schema import GenerateJsonSchema

from app.config.logging.logger import log

# 预算常量
PROMPT_MAX = 3000
TITLE_MAX = 10

# child_agent_id 运行时描述模板：``{ids}`` 在 schema 生成期由进程级注册表的
# ``child_agent_ids()`` 注入，模型据此从合法选项中选择且明确必填。
_CHILD_AGENT_ID_DESCRIPTION_TEMPLATE = (
    "Target child AgentProfile id. REQUIRED. Must be one of the available child agent ids: "
    "{ids}. Pick the child agent whose role fits the task。"
)


def _available_child_agent_ids() -> list[str]:
    """返回进程级注册表中可委派子 Agent 的 ID 列表。

    注册表在应用启动后经 ``set_agent_registry`` 注入；本函数延迟到 schema 生成时调用，
    此时注册表已就绪。未初始化或取数失败时返回空列表（调用方降级为静态描述）。
    """

    try:
        from app.config.configuration import get_agent_registry

        return sorted(get_agent_registry().child_agent_ids())
    except (RuntimeError, ImportError, AttributeError):
        return []


class DelegateTaskArgs(BaseModel):
    """描述父 Agent 指派给子 Agent 的委派契约。

    面向子 Agent 的任务信息统一通过 ``prompt`` 自由文本承载，父 Agent 只需给出
    单一目标文本；``title`` 仅用于展示与可追溯，``child_agent_id`` 决定目标
    child Agent profile。预算校验在 ``model_validator(mode="after")`` 中统一进行，
    超限返回中英文错误信息。
    """

    child_agent_id: str = Field(
        description=_CHILD_AGENT_ID_DESCRIPTION_TEMPLATE
    )
    title: str = Field(
        description=(
            f"Short title for the delegated task, used for display and traceability. "
            f"It must not duplicate the prompt or carry secrets; it is required and "
            f"must be a non-empty, non-whitespace string of at most {TITLE_MAX} characters."
        ),
    )
    prompt: str = Field(
        description=(
            f"The full task contract as free-form text (no fixed format required), passed "
            f"verbatim as the delegated task input. It is required and must be a non-empty "
            f"string of at most {PROMPT_MAX} characters. You may organize it with optional "
            f"markdown sections such as Objective / Rules / References / Expected Output to "
            f"improve clarity, but any clear free-form wording is acceptable."
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

    @classmethod
    def model_json_schema(
        cls,
        by_alias: bool = True,
        ref_template: str = "#/$defs/{model}",
        schema_generator: type[GenerateJsonSchema] = GenerateJsonSchema,
        mode: Literal["validation", "serialization"] = "validation",
    ) -> dict[str, Any]:
        """生成模型可见 JSON schema，并把可用 child agent id 列表注入描述。

        注册表在应用启动后注入，模块加载期不可靠；schema 生成发生在工具注册时，
        此时 ``get_agent_registry().child_agent_ids()`` 可返回真实可用集合，模型据此
        从合法选项中选择，且描述显式标注必填。取数失败则保留静态描述。
        """

        schema = super().model_json_schema(
            by_alias=by_alias,
            ref_template=ref_template,
            schema_generator=schema_generator,
            mode=mode,
        )
        ids = _available_child_agent_ids()
        if ids:
            schema.setdefault("properties", {}).setdefault("child_agent_id", {})[
                "description"
            ] = _CHILD_AGENT_ID_DESCRIPTION_TEMPLATE.format(ids=", ".join(ids))
        return schema
