"""Pydantic arguments for the delegate_task tool.

本模型用三个字段（child_agent_id / agent_name / message）描述父 Agent 指派给子 Agent 的
委派契约。任务内容通过自由文本 ``message`` 承载，父 Agent 不被迫拆成结构化字段；
``message`` 描述不限定章节格式（Objective / Rules / References / Expected Output 等
仅作可选建议，不当作强制结构）。预算校验退化为 ``message`` 总长度硬校验与
``agent_name`` 必填/长度校验，超限时返回明确中英文错误并通过规范日志事件记录超限字段
与实测长度，便于排查父 Agent 调用问题。
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from pydantic.json_schema import GenerateJsonSchema

from app.config.logging.logger import log

# 预算常量
MESSAGE_MAX = 3000
AGENT_NAME_MAX = 10

# child_agent_id 运行时描述模板：``{ids}`` 在 schema 投影期由进程级注册表的
# ``child_agent_ids()`` 注入，模型据此从合法选项中选择且明确必填。合法 id 同时以
# JSON Schema ``enum`` 收口（见 ``model_json_schema``），使模型无法凭语义猜测不存在的
# 角色名（2026-09-17 事故：只能看到未替换的 ``{ids}`` 占位符 ⇒ 猜测 general /
# explorer / architect 等不存在的 id ⇒ 连续 child not found ⇒ run 失败）。
_CHILD_AGENT_ID_DESCRIPTION_TEMPLATE = (
    "Target child AgentProfile id. REQUIRED. Must be one of the available child agent ids: "
    "{ids}. Choose the child whose description best fits the task."
)

# 注册表尚未就绪时的降级文案：明确告知「无法列举且不得猜测」，绝不把模板占位符
# 或不完整信息透给模型（这正是旧实现沉默失效的地方）。
_CHILD_AGENT_ID_DESCRIPTION_UNRESOLVED = (
    "Target child AgentProfile id. REQUIRED. The available child agent ids cannot be "
    "resolved right now, so they are intentionally not listed; do not guess an id."
)


def _available_child_agent_ids() -> list[str]:
    """返回进程级注册表中可委派子 Agent 的 ID 列表。

    注册表在应用启动后经 ``set_agent_registry`` 注入；本函数在 schema 投影期（以及
    参数校验期）调用，此时注册表应已就绪。注册表未初始化**或取数异常**时统一返回空列表，
    并记 WARNING：调用方据此降级为「不可列举」文案——投影位于模型下发热路径，任何异常
    都不得穿透（否则一次注册表故障会炸穿整轮 run）。

    返回:
        按字典序排序的可委派子 Agent id 列表；注册表不可用时为空列表。

    异常:
        无（注册表不可用属预期降级路径，异常在函数内收口）。

    副作用:
        取数失败时写一条 WARNING 日志（``delegate_task_child_agent_catalog_unavailable``）。
    """

    try:
        from app.config.configuration import get_agent_registry

        return sorted(get_agent_registry().child_agent_ids())
    except Exception as exc:
        log.warning(
            "delegate_task_child_agent_catalog_unavailable",
            extra={
                "msg": "子 Agent 候选集取数失败，child_agent_id 已降级为不可列举",
                "data": {
                    "reason": "agent_registry_unavailable",
                    "error_type": type(exc).__name__,
                },
            },
        )
        return []


class DelegateTaskArgs(BaseModel):
    """描述父 Agent 指派给子 Agent 的委派契约。

    面向子 Agent 的任务信息统一通过 ``message`` 自由文本承载，父 Agent 只需给出
    单一目标文本；``agent_name`` 仅用于展示与可追溯，``child_agent_id`` 决定目标
    child Agent profile。预算校验在 ``model_validator(mode="after")`` 中统一进行，
    超限返回中英文错误信息。
    """

    child_agent_id: str = Field(description=_CHILD_AGENT_ID_DESCRIPTION_TEMPLATE)
    #软约束
    agent_name: str = Field(
        description=(
            "Short noun-phrase agent_name for the agent,agent_name 必须是唯一的. "
            f"REQUIRED, at most {AGENT_NAME_MAX} characters — keep it to a few words."
        ),
    )
    message: str = Field(
        description=(
            "The complete task contract as free-form text, passed verbatim as the child's "
            f"entire input. REQUIRED, at most {MESSAGE_MAX} characters."
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
            ValueError: 当 message 超过预算上限或 agent_name 为空/超长时抛出，消息含
                英文错误键与面向模型的中文 reason。

        副作用:
            超限时通过 ``log.warning`` 以 ``delegate_task_args_over_budget`` 事件键记录
            超限字段、上限与实测长度（不记录任何 secret 或敏感信息）。
        """

        # agent_name
        if not self.agent_name or not self.agent_name.strip():
            log.warning(
                "delegate_task_args_over_budget",
                extra={
                    "msg": "delegate_task agent_name 为空或纯空白",
                    "data": {"field": "agent_name"},
                },
            )
            raise ValueError(
                "delegate_task.agent_name_required: 任务标题不能为空或纯空白，"
                "必须提供简洁的任务标题。"
            )
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
                "delegate_task.agent_name_over_budget: 任务标题不得超过 "
                f"{AGENT_NAME_MAX} 个字符（当前 {len(self.agent_name)} 字符）。"
            )

        # message
        if not self.message or not self.message.strip():
            log.warning(
                "delegate_task_args_over_budget",
                extra={
                    "msg": "delegate_task message 为空或纯空白",
                    "data": {"field": "message"},
                },
            )
            raise ValueError(
                "delegate_task.message_required: 任务 message 不能为空或纯空白，"
                "必须提供委派任务内容。"
            )
        if len(self.message) > MESSAGE_MAX:
            log.warning(
                "delegate_task_args_over_budget",
                extra={
                    "msg": "delegate_task message 超出预算上限",
                    "data": {
                        "field": "message",
                        "limit": MESSAGE_MAX,
                        "actual": len(self.message),
                    },
                },
            )
            raise ValueError(
                "delegate_task.message_over_budget: 任务 message 不得超过 "
                f"{MESSAGE_MAX} 个字符（当前 {len(self.message)} 字符）。"
            )

        return self

    @model_validator(mode="after")
    def _validate_child_agent_id(self) -> "DelegateTaskArgs":
        """校验 ``child_agent_id`` 落在注册表真实候选集内，使 enum 从软引导升级为硬约束。

        schema 里的 ``enum`` 只是对模型的软引导；模型（或被误导的调用方）仍可能下发不存在
        的目标，从而在委派业务层才报 ``child not found``。本校验在参数模型层收口，使非法
        目标在下发工具调用时即被拒绝，并把合法候选清单回灌给模型用于自纠。

        候选集取不到（注册表尚未注入或取数异常）时**不拦截**：此时无法裁决合法性，交由委派
        业务层（``unknown_child_agent``）判定，避免把降级场景全量拒绝。

        参数:
            无（基于实例已解析字段校验）。

        返回:
            校验通过的 ``DelegateTaskArgs`` 实例本身。

        异常:
            ValueError: 当 ``child_agent_id`` 不在真实候选集内时抛出；消息携带合法候选清单
                与中文 reason，便于模型一次修正。

        副作用:
            非法取值时通过 ``log.warning`` 以 ``delegate_task_child_agent_id_unknown``
            事件键记录非法取值与候选清单（均为公开 id，不含 secret）。
        """

        candidates = _available_child_agent_ids()
        if not candidates or self.child_agent_id in candidates:
            return self
        log.warning(
            "delegate_task_child_agent_id_unknown",
            extra={
                "msg": "委派目标不在已注册子 Agent 候选集内，已拒绝该次工具调用",
                "data": {"child_agent_id": self.child_agent_id, "candidates": candidates},
            },
        )
        raise ValueError(
            "delegate_task.child_agent_id_unknown: 委派目标 "
            f"'{self.child_agent_id}' 未注册，合法取值为: {', '.join(candidates)}。"
        )

    @classmethod
    def model_json_schema(
        cls,
        by_alias: bool = True,
        ref_template: str = "#/$defs/{model}",
        schema_generator: type[GenerateJsonSchema] = GenerateJsonSchema,
        mode: Literal["validation", "serialization"] = "validation",
    ) -> dict[str, Any]:
        """生成模型可见 JSON schema，把可用 child agent id 收口为 enum 并列举在描述中。

        本方法在**每次向模型投影工具定义时**调用（delegate_task 声明了运行期
        ``schema_provider``），因此注册表注入时机不影响结果——注册期取到的空值不会
        被固化。可用 id 同时进入 ``enum`` 硬约束与 description 文案：模型既无法凭语义
        猜测不存在的角色名，也能直接读到合法候选。注册表尚未就绪时不注入 enum（避免
        空 enum 让所有取值非法），并把描述降级为「不可列举且不得猜测」，绝不暴露模板
        占位符。

        参数:
            by_alias: 是否按别名生成（默认 True，与 pydantic 约定一致）。
            ref_template: ``$ref`` 模板。
            schema_generator: JSON Schema 生成器类型。
            mode: 生成模式（validation / serialization）。

        返回:
            注入真实候选集后的参数字典 schema。

        异常:
            无（注册表取数失败属预期降级路径）。

        副作用:
            候选集不可用时写一条 WARNING 日志（``delegate_task_child_agent_catalog_unavailable``）。
        """

        schema = super().model_json_schema(
            by_alias=by_alias,
            ref_template=ref_template,
            schema_generator=schema_generator,
            mode=mode,
        )
        ids = _available_child_agent_ids()
        child_schema = schema.setdefault("properties", {}).setdefault("child_agent_id", {})
        if ids:
            child_schema["enum"] = list(ids)
            child_schema["description"] = _CHILD_AGENT_ID_DESCRIPTION_TEMPLATE.format(
                ids=", ".join(ids)
            )
        else:
            child_schema["description"] = _CHILD_AGENT_ID_DESCRIPTION_UNRESOLVED
            log.warning(
                "delegate_task_child_agent_catalog_unavailable",
                extra={
                    "msg": "委派目标候选集不可用，child_agent_id 描述已降级为不可列举",
                    "data": {"reason": "agent_registry_not_initialized"},
                },
            )
        return schema
