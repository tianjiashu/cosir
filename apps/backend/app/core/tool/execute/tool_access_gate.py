"""工具调用准入门禁：解析工具定义 + 权限门禁 + 参数校验。

这是工具执行管线（``ToolExecutor.execute``）的第一阶段：把「模型请求的一次工具调用」
翻译为「可以安全交给隔离执行器的工具定义 + 已校验参数」，或一条归一化的拒绝观察。
三段检查（注册表命中 / Agent profile 权限 / 参数 schema）同属「准入决策」这一职责，故收口
在本模块，不在管线门面里平铺。

职责边界：
- 负责：解析工具定义、按 ``allowed_tool_names`` 做 Agent 级门禁、参数解码与校验，并把各类
  拒绝的原因文案归一为面向模型的富文本观察。
- 不负责：隔离执行与硬超时强杀、输出预算（以上归 ``ToolHandlerRunner`` /
  ``ToolObservationBudget``）。
"""

from collections.abc import Collection
from dataclasses import dataclass, field

from app.config.logging.logger import log
from app.core.llm_provider.message_codec import decode_tool_arguments
from app.core.tool.call_intent import split_call_arguments
from app.core.tool.results.tool_error import tool_error
from app.core.tool.schemas import (
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_registry import ToolRegistry
from app.core.tool.validation.arguments import (
    validate_tool_arguments,
)


@dataclass(frozen=True)
class ToolGateOutcome:
    """一次工具调用准入门禁的判定结果。

    字段:
        tool: 通过全部检查的工具定义；被拒时为 ``None``，此时 ``denial`` 非空。
        arguments: 准入后的最终参数字典（已过 schema 校验）；被拒时为空字典。
        denial: 被拒时的归一化 error 观察；通过时为 ``None``。该观察**尚未**过输出
            预算，由 ``ToolExecutor`` 统一应用，保证所有返回路径预算口径一致。
    """

    tool: ToolDefinition | None = None
    arguments: dict[str, object] = field(default_factory=dict)
    denial: ToolObservation | None = None
    intent: str = ""

    @property
    def admitted(self) -> bool:
        """返回本次调用是否通过准入门禁。

        参数:
            无。

        返回:
            ``tool`` 非空且 ``denial`` 为空时返回 ``True``；否则 ``False``。

        异常:
            无。

        副作用:
            无（只读自身字段）。
        """

        return self.tool is not None and self.denial is None


class ToolAccessGate:
    """工具调用准入门禁：判定一次工具调用能否进入隔离执行阶段。"""

    def __init__(self, registry: ToolRegistry) -> None:
        """初始化准入门禁。

        参数:
            registry: 工具注册表，提供工具定义查询。

        返回:
            无。

        异常:
            无。

        副作用:
            持有 ``registry`` 引用，不触发任何工具执行。
        """

        self._registry = registry

    def evaluate(
        self,
        call: ToolCall,
        execution_context: ToolExecutionContext,
        allowed_tool_names: Collection[str] | None = None,
    ) -> ToolGateOutcome:
        """按顺序执行三段准入检查并返回判定结果。

        检查顺序：注册表命中 → Agent profile 权限门禁 → 图片能力（``returns_images`` 的工具要求本次
        Run 的模型支持图片）→ 参数解码与校验。
        任一环节拒绝即短路返回 ``denial``，不继续后续检查、不执行工具。全部通过时返回
        ``admitted=True`` 的判定结果。

        参数:
            call: 模型请求的工具调用，含工具名、原始参数串与调用 id。
            execution_context: 本次执行的运行时边界（会话 / 工作区 / 根路径）。
            allowed_tool_names: 当前 Agent profile 允许运行的工具名；为 None 表示
                调用方不增加 Agent 级门禁。

        返回:
            :class:`ToolGateOutcome`：通过时 ``admitted=True`` 且携带工具定义与最终
            参数；被拒时 ``denial`` 为经 :func:`tool_error` 构造的 error 观察。

        异常:
            无（各类拒绝全部归一化为 denial 观察，不向外抛出）。

        副作用:
            无；只读注册表与执行上下文，不执行工具。
        """

        tool = self._registry.get_tool_definition(call.tool_name)
        if tool is None:
            return ToolGateOutcome(
                denial=tool_error(
                    call.tool_name,
                    f"unknown tool: {call.tool_name}. "
                    "This tool is not registered; call only the tools you were given.",
                    tool_call_id=call.call_id,
                )
            )

        if allowed_tool_names is not None and tool.name not in allowed_tool_names:
            return ToolGateOutcome(
                denial=tool_error(
                    tool.name,
                    # 准入拒绝只给「停止调用」这一个信号：禁用来源（Session 类型规则、派生深度、用户
                    # 关掉的工具组）与持续范围（本 Session / 本轮）由工具准入通知向模型说明，门禁不
                    # 复述来源，也不承诺「本轮之后就能用」；可用工具集也不回灌——模型本就持有工具
                    # 声明，回灌只会放大提示面。
                    f"tool is not allowed: {tool.name}. "
                    "This tool is not available. Do not call it again.",
                    tool_call_id=call.call_id,
                )
            )

        agent = execution_context.agent
        if tool.returns_images and agent is not None and not agent.model.supports_image:
            return ToolGateOutcome(
                denial=tool_error(
                    tool.name,
                    f"{tool.name} returns images, but the current model cannot read images. "
                    "Use a text-only alternative instead of calling it again.",
                    tool_call_id=call.call_id,
                    status_hint="模型不支持图片",
                )
            )

        try:
            intent, arguments = split_call_arguments(decode_tool_arguments(call.arguments))
        except ValueError as exc:
            return ToolGateOutcome(
                denial=tool_error(
                    tool.name,
                    "Invalid tool arguments. "
                    f"{exc}. Supply intent and correct the arguments before retrying.",
                    tool_call_id=call.call_id,
                )
            )

        validation = validate_tool_arguments(
            arguments,
            tool.parameters_schema,
            tool.args_model,
        )
        if not validation.ok:
            return ToolGateOutcome(
                denial=tool_error(
                    tool.name,
                    f"invalid tool arguments: {validation.error}. "
                    "This is deterministic, so fix the argument values/types per "
                    "the tool's parameter schema and call again. The same "
                    "arguments will always be rejected.",
                    tool_call_id=call.call_id,
                )
            )

        if validation.coercions:
            # 输入适配只还原无损形态（如 "30"→30），是模型未按声明类型传值的信号；
            # 只记字段与类型，不记参数值，避免命令/路径正文进入日志。
            log.warning(
                "tool_argument_coerced",
                extra={
                    "msg": "工具参数发生宽容归一（模型未按声明类型传值）",
                    "data": {
                        "tool": tool.name,
                        "tool_call_id": call.call_id,
                        "coercions": [
                            {
                                "field": item.field,
                                "from_type": item.from_type,
                                "to_type": item.to_type,
                            }
                            for item in validation.coercions
                        ],
                    },
                },
            )

        return ToolGateOutcome(tool=tool, arguments=validation.arguments, intent=intent)
