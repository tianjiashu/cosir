"""单个已准备工具调用的 handler 执行器。"""

import logging
from dataclasses import dataclass
from time import monotonic
from typing import Mapping, Optional

from app.domain.artifacts.service import ArtifactService
from app.tools.execution.service import ToolExecutionService
from app.tools.execute import execute_tool_handler
from app.tools.results import ToolObservationBuilder, ToolRuntimeResult
from app.tools.types import ArtifactRequest, ToolDefinition


@dataclass(frozen=True)
class PreparedToolCall:
    """表示已通过策略和审批的单个工具调用。

    参数:
        tool: 已注册工具定义。
        arguments: 已验证的工具参数。
        run_id: 所属 Durable Run 标识。
        step_id: 可选运行时步骤标识。
        tool_call_id: 可选持久化工具调用标识。
        idempotency_key: 稳定幂等键。

    返回:
        不可变已准备调用。

    异常:
        无。

    副作用:
        无。
    """

    tool: ToolDefinition
    arguments: Mapping[str, object]
    run_id: str
    step_id: Optional[str]
    tool_call_id: str
    idempotency_key: str


class ToolCallExecutor:
    """执行一个 handler，并统一写入 artifact 和执行事实。"""

    def __init__(
        self,
        observation_builder: ToolObservationBuilder,
        logger: logging.Logger,
        artifact_service: Optional[ArtifactService] = None,
        execution_service: Optional[ToolExecutionService] = None,
    ) -> None:
        """初始化单调用执行器。

        参数:
            observation_builder: 负责输出摘要的观测构建器。
            logger: 记录可排查执行日志的日志器。
            artifact_service: 可选 artifact 落盘服务。
            execution_service: 可选工具执行事实服务。

        返回:
            无。

        异常:
            无。

        副作用:
            保存执行依赖。
        """

        self._observation_builder = observation_builder
        self._logger = logger
        self._artifact_service = artifact_service
        self._execution_service = execution_service

    def execute(self, prepared: PreparedToolCall) -> ToolRuntimeResult:
        """调用 handler 并归一化结果。

        参数:
            prepared: 已完成注册、校验、策略和审批的工具调用。

        返回:
            含模型观测和可选 artifact 标识的执行结果。

        异常:
            无。handler 失败会转换为错误结果并写入执行记录。

        副作用:
            可能执行 handler 副作用、写入 artifact、更新工具调用状态与执行记录。
        """

        started_at = monotonic()
        self._logger.info(
            "tool_executor_started run_id=%s step_id=%s tool_call_id=%s tool=%s idempotency_key=%s",
            prepared.run_id,
            prepared.step_id,
            prepared.tool_call_id,
            prepared.tool.name,
            prepared.idempotency_key,
        )
        self._mark_running(prepared)
        execution = execute_tool_handler(
            handler=prepared.tool.handler,
            arguments=prepared.arguments,
            timeout_seconds=prepared.tool.timeout_seconds,
        )
        elapsed_ms = int((monotonic() - started_at) * 1000)
        if execution.status == "error":
            return self._handle_failure(prepared, execution.error, elapsed_ms)
        return self._handle_success(prepared, execution.content, elapsed_ms)

    def _mark_running(self, prepared: PreparedToolCall) -> None:
        """在存在持久化记录时推进调用状态。

        参数:
            prepared: 当前已准备调用。

        返回:
            无。

        异常:
            ValueError: 当工具调用状态不能进入 running 时抛出。

        副作用:
            更新 tool_calls 表。
        """

        if self._execution_service is not None and prepared.tool_call_id:
            self._execution_service.mark_running(prepared.tool_call_id)

    def _handle_success(
        self,
        prepared: PreparedToolCall,
        content: object,
        elapsed_ms: int,
    ) -> ToolRuntimeResult:
        """处理成功 handler 输出并按需创建 artifact。

        参数:
            prepared: 当前已准备调用。
            content: handler 返回的文本或 ArtifactRequest。
            elapsed_ms: handler 耗时毫秒数。

        返回:
            成功运行时结果。

        异常:
            OSError: 当 artifact 无法写入时抛出。

        副作用:
            可能写 artifact，并写入工具执行完成记录。
        """

        artifact_id = None
        text_content = content if isinstance(content, str) else str(content)
        if isinstance(content, ArtifactRequest):
            if self._artifact_service is None:
                return self._handle_failure(prepared, "artifact service is not configured", elapsed_ms)
            artifact = self._artifact_service.create_from_request(content, prepared.run_id, prepared.step_id)
            artifact_id = artifact.artifact_id
            text_content = f"{content.summary}\nartifact_id={artifact_id}"
        if self._execution_service is not None and prepared.tool_call_id:
            self._execution_service.mark_completed(prepared.tool_call_id)
            self._execution_service.create_execution(
                prepared.tool_call_id,
                status="succeeded",
                effect_status="completed",
                artifact_id=artifact_id,
            )
        self._logger.info(
            "tool_executor_finished run_id=%s step_id=%s tool_call_id=%s tool=%s idempotency_key=%s elapsed_ms=%s artifact_id=%s",
            prepared.run_id,
            prepared.step_id,
            prepared.tool_call_id,
            prepared.tool.name,
            prepared.idempotency_key,
            elapsed_ms,
            artifact_id,
        )
        return ToolRuntimeResult(
            observation=self._observation_builder.success(
                tool_name=prepared.tool.name,
                content=text_content,
                permission=prepared.tool.permission,
                tool_call_id=prepared.tool_call_id,
                artifact_id=artifact_id,
            ),
            artifact_id=artifact_id,
        )

    def _handle_failure(
        self,
        prepared: PreparedToolCall,
        error: str,
        elapsed_ms: int,
    ) -> ToolRuntimeResult:
        """记录 handler 失败并构造错误观测。

        参数:
            prepared: 当前已准备调用。
            error: 归一化错误文本。
            elapsed_ms: handler 耗时毫秒数。

        返回:
            失败运行时结果。

        异常:
            ValueError: 当持久化状态迁移非法时抛出。

        副作用:
            更新工具调用状态、写入执行记录和错误日志。
        """

        if self._execution_service is not None and prepared.tool_call_id:
            self._execution_service.mark_failed(prepared.tool_call_id)
            self._execution_service.create_execution(
                prepared.tool_call_id,
                status="failed",
                effect_status="not_completed",
                error=error,
            )
        self._logger.error(
            "tool_executor_failed run_id=%s step_id=%s tool_call_id=%s tool=%s idempotency_key=%s elapsed_ms=%s error=%s",
            prepared.run_id,
            prepared.step_id,
            prepared.tool_call_id,
            prepared.tool.name,
            prepared.idempotency_key,
            elapsed_ms,
            error,
        )
        return ToolRuntimeResult(
            observation=self._observation_builder.error(
                tool_name=prepared.tool.name,
                error=error,
                permission=prepared.tool.permission,
                approval_status="allow",
                tool_call_id=prepared.tool_call_id,
            )
        )
