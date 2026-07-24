"""工具调度器：对模型请求的工具调用做权限门禁 + 参数校验 + 隔离执行编排。"""

from collections.abc import Iterable

from app.tools.schemas import ToolCall, ToolDefinition, ToolObservation
from app.tools.tool_execute.tool_error import tool_error
from app.tools.tool_execute.tool_executor import ToolExecutor
from app.tools.tool_registry import ToolRegistry
from app.tools.validation.arguments import validate_tool_arguments


class ToolScheduler:
    """模型请求工具调用的统一调度入口。

    单一职责：对模型请求的工具调用做「权限策略校验 + 参数校验 + 隔离执行」
    的三段式编排，并始终返回归一化的 :class:`ToolObservation`，使上层
    （workflow / 运行时）无需关心失败原因细节。

    职责边界：
    - 负责：从注册表解析工具定义、按 ``allowed_permissions`` 做权限门禁、
      调用 ``validation`` 做参数校验、委派 :class:`ToolExecutor` 隔离执行。
    - 不负责：子进程隔离与超时强杀（``ToolExecutor``）、handler 业务逻辑、
      跨进程日志桥接、模型可见性之外的运行策略。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        allowed_permissions: Iterable[str],
        executor: ToolExecutor | None = None,
    ) -> None:
        """初始化调度器并固化权限策略。

        参数:
            registry: 工具注册表，提供工具定义查询。
            allowed_permissions: 当前运行上下文允许的工具权限集合；不在其中
                的工具调用将被拒绝。
            executor: 可选的执行器实例；缺省时新建一个 :class:`ToolExecutor`。

        返回:
            无。

        异常:
            无。

        副作用:
            持有 ``registry``、``allowed_permissions``（转为 set 去重）与
            ``executor`` 引用；不触发任何工具执行。
        """

        self._registry = registry
        self._allowed_permissions = set(allowed_permissions)
        self._executor = executor or ToolExecutor()

    def list_model_visible_tools(self) -> list[ToolDefinition]:
        """返回当前权限策略下对模型可见的工具定义列表。

        参数:
            无。

        返回:
            工具定义列表，仅包含 ``visible_by_default`` 为真且权限在
            ``allowed_permissions`` 内的工具。

        异常:
            无。

        副作用:
            无（只读注册表与权限集合）。
        """

        return [
            tool
            for tool in self._registry.get_all_definitions()
            if tool.visible_by_default and tool.permission in self._allowed_permissions
        ]

    def get_tool_definition(self, tool_name: str) -> ToolDefinition | None:
        """按名称查询已注册的工具定义。

        参数:
            tool_name: 工具名称。

        返回:
            命中的 :class:`ToolDefinition`；未注册时返回 None。

        异常:
            无。

        副作用:
            无（只读注册表）。
        """

        return self._registry.get_tool_definition(tool_name)

    def execute(self, call: ToolCall) -> ToolObservation:
        """执行单次工具调用并返回归一化观察结果。

        编排顺序：注册表命中 → 权限门禁 → 参数校验 → 委派执行器隔离执行；
        任一前置环节失败都直接返回带 ``reason`` 的 :class:`ToolObservation`，
        绝不抛出，使上层始终拿到可落库/可回传的结果。

        参数:
            call: 模型请求的工具调用，含工具名、参数与调用 id。

        返回:
            归一化后的 :class:`ToolObservation`：成功为 status="success"；
            未知工具 / 权限拒绝 / 参数非法 / 执行失败为 status="error"，
            并通过 ``reason`` 区分（unknown_tool / permission_denied /
            invalid_arguments / handler_exception / timeout 等）。

        异常:
            无（所有失败路径均归一化为 error 观察）。

        副作用:
            委派 :class:`ToolExecutor` 启动子进程执行；可能因权限或参数
            校验失败而短路返回，不进入执行阶段。
        """

        tool = self._registry.get_tool_definition(call.tool_name)
        if tool is None:
            return tool_error(
                call.tool_name,
                f"unknown tool: {call.tool_name}",
                reason="unknown_tool",
                tool_call_id=call.call_id,
            )
        if tool.permission not in self._allowed_permissions:
            return tool_error(
                tool.name,
                f"permission denied for tool: {tool.name}",
                reason="permission_denied",
                permission=tool.permission,
                tool_call_id=call.call_id,
            )

        validation = validate_tool_arguments(
            call.arguments,
            tool.parameters_schema,
            tool.args_model,
            tuple(tool.required_params),
        )
        if not validation.ok:
            return tool_error(
                tool.name,
                f"invalid tool arguments: {validation.error}",
                reason="invalid_arguments",
                permission=tool.permission,
                tool_call_id=call.call_id,
            )
        return self._executor.execute(tool, validation.arguments, tool_call_id=call.call_id)
