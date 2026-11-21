"""工具调用准入门禁：解析工具定义 + 权限门禁 + 参数校验。

这是工具执行管线（``ToolExecutor.execute``）的第一阶段：把「模型请求的一次工具调用」
翻译为「可以安全交给隔离执行器的工具定义 + 已校验参数」，或一条归一化的拒绝观察。
三段检查（工具定义命中 / 本次请求的准入名单 / 参数 schema）同属「准入决策」这一职责，故收口
在本模块，不在管线门面里平铺。

工具定义有两个来源：进程级注册表（内置工具）与本次请求工具集里已加载的外部工具（``RequestTools.external``，
服务给的 JSON Schema）。两者走同一套准入顺序，只是参数校验依据不同；还没加载就调用的外部工具在这里拦下，
告诉模型先用 ``tool_search`` 加载。

职责边界：
- 负责：解析工具定义、按本次请求的准入名单（``RequestTools.admitted``）门禁、参数解码与校验（规则在
  ``validation.call_arguments``，与其他需要业务参数的地方同源），并把各类拒绝的原因文案归一为
  面向模型的富文本观察。
- 不负责：handler 执行、输出预算（以上归 ``ToolExecutor`` / ``ToolOutputBudget``）。
"""

from dataclasses import dataclass, field

from app.config.logging.logger import log
from app.core.tool.external_tools import ExternalCatalogUnavailable, external_tools
from app.core.tool.results.tool_error import tool_error
from app.core.tool.schemas import (
    TOOL_SEARCH,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.schemas.request_tools import RequestTools
from app.core.tool.tool_registry import ToolRegistry
from app.core.tool.validation.call_arguments import (
    CallArguments,
    CallArgumentsError,
    resolve_call_arguments,
    resolve_external_arguments,
)
from app.models.external_tool import ExternalToolDefinition


@dataclass(frozen=True)
class ToolGateOutcome:
    """一次工具调用准入门禁的判定结果。

    字段:
        tool: 通过全部检查的内置工具定义；延迟工具或被拒时为 ``None``。
        external: 通过全部检查的外部工具定义快照；内置工具或被拒时为 ``None``。通过时两者恰有一个非空。
        arguments: 准入后的最终参数字典（已过 schema 校验）；被拒时为空字典。
        denial: 被拒时的归一化 error 观察；通过时为 ``None``。该观察**尚未**过输出
            预算，由 ``ToolExecutor`` 统一应用，保证所有返回路径预算口径一致。
    """

    tool: ToolDefinition | None = None
    external: ExternalToolDefinition | None = None
    arguments: dict[str, object] = field(default_factory=dict)
    denial: ToolObservation | None = None


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
        request_tools: RequestTools,
    ) -> ToolGateOutcome:
        """按顺序执行三段准入检查并返回判定结果。

        检查顺序：工具定义命中（注册表，其次已加载的延迟工具）→ 本次请求准入 → 参数解码与校验。
        任一环节拒绝即短路返回 ``denial``，不继续后续检查、不执行工具。全部通过时返回
        ``admitted=True`` 的判定结果。

        参数:
            call: 模型请求的工具调用，含工具名、原始参数串与调用 id。
            execution_context: 本次执行的运行时边界（会话 / 工作区 / 根路径）。
            request_tools: 发出这次请求时的工具集；不在 ``admitted`` 里的工具被拒绝。

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
            external = request_tools.external.get(call.tool_name)
            if external is None:
                return ToolGateOutcome(denial=self._unknown(call, execution_context, request_tools))
            return self._admit_external(call, external)

        if tool.name in request_tools.unloaded:
            return ToolGateOutcome(denial=self._not_loaded(call))
        if tool.name not in request_tools.admitted:
            return ToolGateOutcome(
                denial=tool_error(
                    tool.name,
                    # 准入拒绝只给「停止调用」这一个信号：禁用来源（Session 类型规则、派生深度）与
                    # 持续范围由工具准入通知向模型说明，门禁不复述来源，也不承诺「之后就能用」；可用工具集也不回灌——模型本就持有工具
                    # 声明，回灌只会放大提示面。
                    f"tool is not allowed: {tool.name}. "
                    "This tool is not available. Do not call it again.",
                    tool_call_id=call.call_id,
                )
            )

        try:
            resolved = resolve_call_arguments(tool, call.arguments)
        except CallArgumentsError as exc:
            return ToolGateOutcome(denial=tool_error(tool.name, str(exc), tool_call_id=call.call_id))
        ToolAccessGate._log_resolution(tool.name, call.call_id, resolved)
        return ToolGateOutcome(tool=tool, arguments=resolved.arguments)

    @staticmethod
    def _admit_external(call: ToolCall, external: ExternalToolDefinition) -> ToolGateOutcome:
        """已加载的外部工具：能出现在 ``RequestTools.external`` 里就已通过准入名单（禁用的不在里面），只剩参数校验。"""
        try:
            resolved = resolve_external_arguments(external, call.arguments)
        except CallArgumentsError as exc:
            return ToolGateOutcome(denial=tool_error(external.name, str(exc), tool_call_id=call.call_id))
        ToolAccessGate._log_resolution(external.name, call.call_id, resolved)
        return ToolGateOutcome(external=external, arguments=resolved.arguments)

    @staticmethod
    def _not_loaded(call: ToolCall) -> ToolObservation:
        """延迟工具还没加载（内置与外部同一句）：教模型先用 ``tool_search`` 取定义。"""
        name = call.tool_name
        return tool_error(
            name,
            f"{name} is a deferred tool and its schema is not loaded, so it cannot be called yet. "
            f'Load it with {TOOL_SEARCH} using query "select:{name}", then call it again.',
            tool_call_id=call.call_id,
            status_hint="工具未加载",
        )

    @staticmethod
    def _unknown(
        call: ToolCall, execution_context: ToolExecutionContext, request_tools: RequestTools
    ) -> ToolObservation:
        """不认识的名字：是还没加载的外部工具就教模型怎么加载，否则说它不存在。

        只有本次请求带着 ``tool_search`` 时才问目录：没有检索工具的 Session 里不存在「先加载」这条路，
        端口也可能根本没装配。
        """
        name = call.tool_name
        if TOOL_SEARCH in request_tools.admitted and ToolAccessGate._in_catalog(name, execution_context):
            return ToolAccessGate._not_loaded(call)
        return tool_error(
            name,
            f"unknown tool: {name}. This tool is not registered; call only the tools you were given.",
            tool_call_id=call.call_id,
        )

    @staticmethod
    def _in_catalog(name: str, execution_context: ToolExecutionContext) -> bool:
        try:
            catalog = external_tools().catalog(str(execution_context.workspace_root))
        except ExternalCatalogUnavailable:
            return False
        return any(definition.name == name for definition in catalog)


    @staticmethod
    def _log_resolution(tool_name: str, call_id: str, resolved: CallArguments) -> None:
        if resolved.coercions:
            # 输入适配只还原无损形态（如 "30"→30），是模型未按声明类型传值的信号；
            # 只记字段与类型，不记参数值，避免命令/路径正文进入日志。
            log.warning(
                "tool_argument_coerced",
                extra={
                    "msg": "工具参数发生宽容归一（模型未按声明类型传值）",
                    "data": {
                        "tool": tool_name,
                        "tool_call_id": call_id,
                        "coercions": [
                            {"field": item.field, "from_type": item.from_type, "to_type": item.to_type}
                            for item in resolved.coercions
                        ],
                    },
                },
            )
