"""在注册表边界之上校验权限并归一化观测结果的工具调度器。"""

from collections.abc import Mapping
import logging
from typing import Iterable, List

from app.tools.execution import execute_tool_handler
from app.tools.approval import ToolApprovalPolicy
from app.tools.registry import ToolRegistry
from app.tools.schema import validate_tool_arguments
from app.tools.types import ToolCall, ToolDefinition, ToolObservation


class ToolScheduler:
    """通过一个注册表边界校验并执行工具调用。"""

    def __init__(
        self,
        registry: ToolRegistry,
        allowed_permissions: Iterable[str],
        logger: logging.Logger,
        approval_required_permissions: Iterable[str] = (),
    ) -> None:
        """初始化调度器依赖项。

        参数:
            registry: 用于解析工具名的工具注册表。
            allowed_permissions: 本调度器允许的权限级别。
            logger: 用于执行审计记录的日志记录器。
            approval_required_permissions: 需要用户审批的权限级别。

        返回:
            无。

        异常:
            无。

        副作用:
            保存调度器依赖项。
        """

        self._registry = registry
        self._approval_policy = ToolApprovalPolicy(
            auto_approved_permissions=allowed_permissions,
            approval_required_permissions=approval_required_permissions,
        )
        self._logger = logger

    def list_model_visible_tools(self) -> List[ToolDefinition]:
        """返回本调度器对模型可见的工具。

        参数:
            无。

        返回:
            根据调度器的模型可见性策略过滤后的已注册工具。

        异常:
            无。

        副作用:
            无。
        """

        return [
            tool
            for tool in self._registry.list_tools()
            if self._approval_policy.is_visible_to_model(tool)
        ]

    def execute(self, call: ToolCall) -> ToolObservation:
        """在校验之后执行一次工具调用。

        参数:
            call: 由模型响应请求的工具调用。

        返回:
            带有成功或错误状态的归一化工具观测结果。

        异常:
            无。工具错误会被捕获进返回的观测结果中。

        副作用:
            可能执行工具处理函数的副作用并写入审计日志。
        """

        try:
            tool = self._registry.get(call.tool_name)
        except KeyError:
            self._logger.warning("tool_missing tool=%s", call.tool_name)
            return ToolObservation(
                tool_name=call.tool_name,
                status="error",
                content="",
                error=f"unknown tool: {call.tool_name}",
            )

        approval = self._approval_policy.decide(tool)
        if approval.status == "denied":
            self._logger.warning(
                "tool_permission_denied tool=%s permission=%s",
                tool.name,
                tool.permission,
            )
            return ToolObservation(
                tool_name=tool.name,
                status="error",
                content="",
                error=approval.reason,
                permission=tool.permission,
                approval_status=approval.status,
            )
        if approval.status == "approval_required":
            self._logger.warning(
                "tool_approval_required tool=%s permission=%s",
                tool.name,
                tool.permission,
            )
            return ToolObservation(
                tool_name=tool.name,
                status="approval_required",
                content="",
                error=approval.reason,
                permission=tool.permission,
                approval_status=approval.status,
            )

        if not isinstance(call.arguments, Mapping):
            self._logger.warning(
                "tool_invalid_args tool=%s error=arguments_not_object",
                tool.name,
            )
            return ToolObservation(
                tool_name=tool.name,
                status="error",
                content="",
                error="invalid tool arguments: expected object",
                permission=tool.permission,
                approval_status=approval.status,
            )

        missing = [name for name in tool.required_params if name not in call.arguments]
        if missing:
            self._logger.warning("tool_invalid_args tool=%s missing=%s", tool.name, missing)
            return ToolObservation(
                tool_name=tool.name,
                status="error",
                content="",
                error=f"missing required parameters: {', '.join(missing)}",
                permission=tool.permission,
                approval_status=approval.status,
            )

        schema_error = validate_tool_arguments(call.arguments, tool.parameters_schema)
        if schema_error:
            self._logger.warning(
                "tool_schema_invalid tool=%s error=%s",
                tool.name,
                schema_error,
            )
            return ToolObservation(
                tool_name=tool.name,
                status="error",
                content="",
                error=f"invalid tool arguments: {schema_error}",
                permission=tool.permission,
                approval_status=approval.status,
            )

        execution = execute_tool_handler(
            handler=tool.handler,
            arguments=call.arguments,
            timeout_seconds=tool.timeout_seconds,
        )
        if execution.error.startswith("tool timed out"):
            self._logger.error(
                "tool_timeout tool=%s timeout_seconds=%s",
                tool.name,
                tool.timeout_seconds,
            )
            return ToolObservation(
                tool_name=tool.name,
                status="error",
                content="",
                error=execution.error,
                permission=tool.permission,
                approval_status=approval.status,
            )
        if execution.status == "error":
            self._logger.error("tool_failed tool=%s error=%s", tool.name, execution.error)
            return ToolObservation(
                tool_name=tool.name,
                status="error",
                content="",
                error=execution.error,
                permission=tool.permission,
                approval_status=approval.status,
            )

        self._logger.info("tool_finished tool=%s", tool.name)
        return ToolObservation(
            tool_name=tool.name,
            status="success",
            content=execution.content,
            permission=tool.permission,
            approval_status=approval.status,
        )
