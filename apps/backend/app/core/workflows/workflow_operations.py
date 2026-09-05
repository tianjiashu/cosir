"""Runtime operations exposed to workflow strategies."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

from langchain_core.messages import ToolMessage
from langgraph.config import get_stream_writer

from app.config.logging.logger import log
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.runtime.tool_execution import ToolTraceRecorder
from app.core.runtime.tool_execution.run_result import ToolRunResult
from app.core.runtime.tool_execution.tool_execution_service import ToolExecutionService
from app.core.tools.schemas import ToolCall, ToolDefinition, ToolExecutionContext, ToolObservation
from app.core.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies
from app.core.tools.tool_execute.tool_scheduler import ToolScheduler
from app.core.workflows.conversation_run_usage_stats import ConversationRunUsageStats
from app.core.workflows.event import RunStatusChangedEvent
from app.models import ConversationRunRecord, ConversationRunStatus, TaskRecord, WorkspaceRecord
from app.service.depends import (
    get_conversation_event_projector,
    get_conversation_run_service,
)

if TYPE_CHECKING:
    from app.core.agents.agent_profile import AgentProfile


class WorkflowOperations:
    """Expose runtime-owned side effects through a narrow workflow boundary.

    状态单一事实来源是 ``ConversationRun``：本门面暴露的状态和当前 run 方法都作用于
    run，不再写 task 执行态（task 执行态由最新 run 派生）。
    """

    def __init__(
        self,
        tool_scheduler: ToolScheduler,
        agent_profile: AgentProfile,
        current_run: ConversationRunRecord,
        current_task: TaskRecord,
        current_workspace: WorkspaceRecord,
        model_tools: list[ToolDefinition] | None = None,
        execution_context: ToolExecutionContext | None = None,
        runtime_dependencies: ToolRuntimeDependencies | None = None,
        tool_trace_recorder: ToolTraceRecorder | None = None,
    ) -> None:
        """初始化运行时操作门面及其私有协作者。

        参数:
            tool_scheduler: 工具调度器（已按 workspace 边界解析或进程级兜底）。
            agent_profile: 驱动本次 run 的 agent profile。
            current_run: 当前绑定的 Conversation Run 记录（门面状态单一事实来源）。
            current_task: 当前执行的任务记录。
            current_workspace: 当前工作区记录。
            model_tools: 暴露给模型的工具定义列表。
            execution_context: 当前执行的运行时边界；为 None 时 ``run_tool_calls``
                日志不注入 ``workspace_id``。
            runtime_dependencies: 可选的本 run 工具运行期依赖；存在 execution_context 时
                会合并进 ``ToolExecutionContext.runtime_dependencies`` 透传给 handler。
            tool_trace_recorder: 可选的工具调用 trace 记录器（依赖倒置）；为 None 时
                工具执行不产生 trace，行为与集成前一致。

        返回:
            无。

        异常:
            无。

        副作用:
            构造 ``ToolExecutionService``、存储执行上下文和 canonical writer，记初始化日志。
        """

        self._conversation_run_state_service = get_conversation_run_service()
        self._event_projector = get_conversation_event_projector()
        self.model_tools: list[ToolDefinition] = list(model_tools or [])
        self.agent_profile = agent_profile
        self._current_workspace = current_workspace
        self._current_run = current_run
        self._current_task = current_task
        self._execution_context = (
            replace(execution_context, runtime_dependencies=runtime_dependencies)
            if execution_context is not None and runtime_dependencies is not None
            else execution_context
        )
        self._tool_service = ToolExecutionService(
            scheduler=tool_scheduler,
            agent_id=agent_profile.agent_id,
            allowed_tool_names=(tool.name for tool in self.model_tools),
            tool_definitions=self.model_tools,
            trace_recorder=tool_trace_recorder,
            should_cancel=self.is_current_run_cancelled,
        )

        log.info(
            "runtime_ops_initialized",
            extra={
                "msg": f"运行时操作门面已初始化，agent_id={agent_profile.agent_id}",
                "data": {
                    "agent_id": agent_profile.agent_id,
                    "model_tools_count": len(self.model_tools),
                    "current_run_id": current_run.id if current_run else None,
                    "current_run_bound": bool(current_run),
                },
            },
        )

    def get_current_run(self) -> ConversationRunRecord:
        """Return the Conversation Run identified by ``current_run_id``.

        用于工作流取「当前要跑的 run」。
        """

        return self._current_run

    def get_current_task(self) -> TaskRecord:
        """Return the task identified by ``current_task_id``.

        用于工作流取「当前要跑的任务」.
        """

        return self._current_task

    def get_current_workspace(self) -> WorkspaceRecord:
        """Return the workspace identified by ``current_workspace_id``.

        用于工作流取「当前要跑的任务」.
        """

        return self._current_workspace

    def process_event(self, event: object) -> object | None:
        """把 workflow event 交给唯一的 snapshot projector。"""

        return self._event_projector.process(event)

    def is_current_run_cancelled(self) -> bool:
        """Return whether the currently bound run should stop.

        取消检测只读进程内取消注册表（运行时信号源）：取消入口由
        ``ConversationRunExecutor.cancel`` 统一标记信号并落库，本方法不做 DB 兜底查询，
        避免协作取消检查在 process 模式工具的 50ms 轮询中产生高频数据库读。

        参数:
            无。

        返回:
            当前 run 已被取消时返回 True；未绑定 run 或无取消信号时返回 False。

        异常:
            无。

        副作用:
            无。
        """
        current_run = self._current_run
        if current_run is None:
            return False
        return cancellation_registry.is_cancelled(str(current_run.id))

    def complete_run_if_running(
        self, usage_stats: ConversationRunUsageStats | None = None
    ) -> ConversationRunRecord | None:
        """Complete the Conversation Run only if it is still running.

        参数:
            run_id: 待完成的 Conversation Run 标识。

        返回:
            成功完成时返回更新后的 ConversationRunRecord；run 已被取消/失败/完成时返回 None。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时同事务写入 completed 状态和回复文本。
        """
        run_id = self._current_run.id
        log.info(
            "run_completion_attempted",
            extra={
                "msg": f"尝试完成 running run，run_id={run_id}",
                "data": {"run_id": run_id},
            },
        )
        record = self._conversation_run_state_service.complete_run_if_running(run_id)
        if record is not None:
            get_stream_writer()(
                RunStatusChangedEvent(
                    task_id=self._current_task.id,
                    run_id=run_id,
                    status=ConversationRunStatus.COMPLETED,
                    usage_stats=usage_stats,
                )
            )
        return record

    def fail_run_if_running(
        self, end_reason: str | None = None, usage_stats: ConversationRunUsageStats | None = None
    ) -> ConversationRunRecord | None:
        """Fail the Conversation Run only if it is still running.

        参数:
            run_id: 待失败落定的 Conversation Run 标识。
            end_reason: 可选失败原因。

        返回:
            成功失败落定时返回更新后的 ConversationRunRecord；run 已不是 running 时返回 None。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时写入 failed 状态。
        """
        run_id = self._current_run.id
        log.info(
            "run_failure_attempted",
            extra={
                "msg": f"尝试将 running run 标记为 failed，run_id={run_id}",
                "data": {"run_id": run_id, "end_reason": end_reason},
            },
        )
        record = self._conversation_run_state_service.fail_run_if_running(run_id, end_reason)
        if record is not None:
            get_stream_writer()(
                RunStatusChangedEvent(
                    task_id=self._current_task.id,
                    run_id=run_id,
                    status=ConversationRunStatus.FAILED,
                    end_reason=end_reason,
                    usage_stats=usage_stats,
                )
            )
        return record

    def cancel_run_if_running(
        self,
        end_reason: str = "runtime_cancelled",
        usage_stats: ConversationRunUsageStats | None = None,
    ) -> ConversationRunRecord | None:
        """Cancel the Conversation Run through the canonical writer if it is still active.

        参数:
            run_id: 待取消的 Conversation Run 标识。
            end_reason: 稳定的取消原因。

        返回:
            成功取消时返回更新后的 ConversationRunRecord；终态已由其它路径落定时返回 None。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            通过 canonical writer 条件事务将运行和助手消息一并标记为 cancelled。
        """
        run_id = self._current_run.id
        log.info(
            "run_cancel_attempted",
            extra={
                "msg": f"尝试将 running run 标记为 cancelled，run_id={run_id}",
                "data": {"run_id": run_id, "end_reason": end_reason},
            },
        )
        record = self._conversation_run_state_service.cancel_run_if_running(run_id, end_reason)
        if record is not None:
            get_stream_writer()(
                RunStatusChangedEvent(
                    task_id=self._current_task.id,
                    run_id=run_id,
                    status=ConversationRunStatus.CANCELLED,
                    end_reason=end_reason,
                    usage_stats=usage_stats,
                )
            )
        return record

    def run_tool_calls(
        self,
        task_id: str,
        calls: list[ToolCall],
        step_id: str | None = None,
        running_loop: asyncio.AbstractEventLoop | None = None,
    ) -> ToolRunResult:
        """Execute model-requested tool calls through the tool system.

        工具生命周期通过明确的 callback 写入 canonical conversation facts。门面持有的
        ``execution_context`` 在内部透传给执行链，最终在执行期注入各 handler。

        参数:
            task_id: 当前任务标识符。
            calls: 模型请求的工具调用列表。
            step_id: 请求这些工具调用的步骤标识符。
            running_loop: 异步工具节点提供的事件循环，用于工具执行期输出桥接。
        返回:
            工具观察结果与供下一步模型使用的消息。
        """

        effective_step_id = step_id or "step"
        calls = [
            replace(
                call,
                call_id=call.call_id or f"{self._current_run.id}:{effective_step_id}:{index}",
            )
            for index, call in enumerate(calls)
        ]
        self._pre_process_run(task_id=task_id, calls=calls, step_id=step_id)

        execution_context = self._execution_context
        if running_loop is None:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
        if execution_context is not None and running_loop is not None:
            runtime_dependencies = replace(
                execution_context.runtime_dependencies,
                runtime_event_loop=running_loop,
            )
            execution_context = replace(
                execution_context,
                runtime_dependencies=runtime_dependencies,
            )

        result: ToolRunResult = self._tool_service.run_calls(
            step_id=effective_step_id,
            calls=calls,
            execution_context=execution_context,
            running_loop=running_loop,
        )

        self._post_process_run(task_id=task_id, step_id=step_id, result=result)

        return result

    def _pre_process_run(
        self,
        task_id: str,
        calls: list[ToolCall],
        step_id: str | None = None,
    ) -> None:
        """Log metadata before dispatching a tool-call batch.

        参数:
            task_id: 当前任务标识。
            calls: 待派发的工具调用列表。
            step_id: 可选步骤标识。

        返回:
            无。

        异常:
            无。

        副作用:
            写入工具批次派发日志。
        """

        workspace_id = self._current_workspace.id if self._current_workspace else None

        log.info(
            "tool_calls_dispatched",
            extra={
                "msg": f"派发 {len(calls)} 个工具调用，step_id={step_id}",
                "data": {
                    "task_id": task_id,
                    "step_id": step_id,
                    "call_count": len(calls),
                    "tool_names": [call.tool_name for call in calls],
                    "workspace_id": workspace_id,
                },
            },
        )

    def _post_process_run(
        self,
        task_id: str,
        result: ToolRunResult,
        step_id: str | None = None,
    ) -> None:
        """Log metadata after a tool-call batch completes.

        参数:
            task_id: 当前任务标识。
            result: 工具批次执行结果。
            step_id: 可选步骤标识。

        返回:
            无。

        异常:
            无。

        副作用:
            写入工具批次完成日志。
        """

        workspace_id = self._current_workspace.id if self._current_workspace else None
        status_counts: dict[str, int] = {}
        error_count = 0
        for obs in result.observations:
            status_counts[obs.status] = status_counts.get(obs.status, 0) + 1
            if obs.status == "error":
                error_count += 1

        log.info(
            "tool_calls_completed",
            extra={
                "msg": (
                    f"工具批次执行完成：{len(result.observations)} 个观察，"
                    f"其中 {error_count} 个失败"
                ),
                "data": {
                    "task_id": task_id,
                    "step_id": step_id,
                    "observation_count": len(result.observations),
                    "status_counts": status_counts,
                    "error_count": error_count,
                    "workspace_id": workspace_id,
                },
            },
        )

    def _to_model_message(self, observation: ToolObservation) -> ToolMessage:
        """把工具观察序列化为模型可见的 ``role="tool"`` 消息（markdown 结构）。

        ``content_text`` 以 markdown 区块组织**对模型可见**的字段：``## Tool`` 承载
        ``tool_name`` / ``status`` / ``retryable``，``## Output`` 承载 ``content``，
        ``## Error`` 承载 ``error``，``## Reason`` 承载 ``reason``；空值跳过对应区块。
        ``tool_call_id``（置于 ``metadata``）、``data``、``permission`` 对模型不可见。

        参数:
            observation: 已产出的工具观察（含正常结果、错误占位、取消占位）。

        返回:
            可并入模型上下文的 ``ToolMessage``，其 ``tool_call_id`` 用于与
            ``AIMessage.tool_calls`` 配对闭合。

        异常:
            无。

        副作用:
            无（不修改入参观察对象）。
        """
        # 仅向模型暴露面向人读的文本通道（content / error / reason）与执行元信息
        # （tool_name / status / retryable）。tool_call_id / data / permission 对模型不可见
        # （前者在 metadata、后者转模型前已被 clear_display_data 清空）。None 与空串视为
        # 无信息，跳过对应区块。其余字段以 markdown 结构组织，使模型能区分「元信息 / 输出 /
        # 错误 / 修正建议」四个语义维度。
        sections: list[str] = []
        meta_lines: list[str] = []
        if observation.tool_name:
            meta_lines.append(f"- name: {observation.tool_name}")
        if observation.status:
            meta_lines.append(f"- status: {observation.status}")
        meta_lines.append(f"- retryable: {observation.retryable}")
        if meta_lines:
            sections.append("## Tool\n\n" + "\n".join(meta_lines))
        if observation.content:
            sections.append(f"## Output\n\n{observation.content}")
        if observation.error:
            sections.append(f"## Error\n\n{observation.error}")
        if observation.reason:
            sections.append(f"## Reason\n\n{observation.reason}")
        content_text = "\n\n".join(sections)
        return ToolMessage(
            content=content_text, tool_call_id=observation.tool_call_id, status=observation.status
        )
