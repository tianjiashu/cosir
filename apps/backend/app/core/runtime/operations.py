"""暴露给工作流策略的运行时操作。"""

import logging
from typing import AsyncIterator, Callable, List, Optional

from app.core.agents.profile import AgentProfile
from app.config.settings import BackendSettings
from app.storage.snapshot import build_checkpoint_snapshot
from app.context.builder import TextContextBuilder
from app.context.budget import validate_context_budget
from app.events.types import EventType, RuntimeEvent
from app.models.base import ModelDelta, RuntimeMessage, StreamingModelAdapter
from app.core.runtime.model_tools import build_model_tool_definitions
from app.storage.records import StepRecord, TaskRecord, TurnRecord
from app.tools.runtime.compatibility import ToolScheduler
from app.tools.runtime.platform import ToolExecutionContext, ToolRuntime
from app.tools.types import ToolCall, ToolDefinition, ToolObservation


class RuntimeOperations:
    """通过一个狭窄的工作流边界暴露运行时拥有的副作用。"""

    def __init__(
        self,
        settings: BackendSettings,
        task_store,
        context_builder: TextContextBuilder,
        model_adapter: StreamingModelAdapter,
        tool_scheduler: ToolScheduler,
        logger: logging.Logger,
        agent_profile: AgentProfile,
        record_event: Callable[[EventType, str, dict], RuntimeEvent],
        tool_runtime: Optional[ToolRuntime] = None,
        tool_run_id: str = "",
    ) -> None:
        """初始化工作流操作门面。

        参数:
            settings: 控制执行限制的运行时配置。
            task_store: 由运行时拥有的存储实现。
            context_builder: 用于创建模型消息的构建器。
            model_adapter: 由运行时拥有的流式模型适配器。
            tool_scheduler: 用于执行模型请求的工具的调度器。
            logger: 用于运行时自有诊断信息的日志记录器。
            agent_profile: 本次运行时执行使用的 Agent 档案。
            record_event: 持久化并记录事件的运行时回调。
            tool_runtime: 可选的 Tool v2 唯一执行入口。
            tool_run_id: 当前任务关联的 Durable Run 标识。

        返回:
            无。

        异常:
            无。

        副作用:
            保存对运行时依赖项的引用。
        """

        self.settings = settings
        self._task_store = task_store
        self._context_builder = context_builder
        self._model_adapter = model_adapter
        self._tool_scheduler = tool_scheduler
        self._logger = logger
        self._agent_profile = agent_profile
        self._record_event = record_event
        self._tool_runtime = tool_runtime
        self._tool_run_id = tool_run_id

    def get_turn_for_task(self, task_id: str) -> TurnRecord:
        """返回任务的持久化轮次。

        参数:
            task_id: 需要获取轮次的任务标识符。

        返回:
            匹配的轮次记录。

        异常:
            KeyError: 如果任务或轮次不存在。

        副作用:
            无。
        """

        return self._task_store.get_turn_for_task(task_id)

    def build_messages(self, task: TaskRecord) -> List[RuntimeMessage]:
        """为任务构建与模型无关的消息。

        参数:
            task: 正在执行的任务记录。

        返回:
            已准备好交给模型适配器的运行时消息。

        异常:
            无。

        副作用:
            无。
        """

        return self._context_builder.build_messages(task, self._agent_profile)

    def create_step(
        self,
        turn_id: str,
        step_type: str,
        status: str,
        input_summary: str,
    ) -> StepRecord:
        """创建一个持久化的运行时步骤。

        参数:
            turn_id: 拥有该步骤的轮次标识符。
            step_type: 稳定的步骤类型，例如 ``model_call`` 或 ``tool_call``。
            status: 步骤的初始状态。
            input_summary: 对步骤输入的简短诊断摘要。

        返回:
            已创建的步记录。

        异常:
            KeyError: 如果轮次不存在。

        副作用:
            向存储写入一行步骤记录。
        """

        return self._task_store.create_step(
            turn_id=turn_id,
            step_type=step_type,
            status=status,
            input_summary=input_summary,
        )

    def update_step(
        self,
        step_id: str,
        status: str,
        output_summary: str = "",
        error: Optional[str] = None,
    ) -> StepRecord:
        """更新一个持久化的运行时步骤状态。

        参数:
            step_id: 需要修改的步骤标识符。
            status: 新的步骤状态。
            output_summary: 可选的、简短的诊断输出摘要。
            error: 可选的错误文本。

        返回:
            已更新的步骤记录。

        异常:
            KeyError: 如果步骤不存在。

        副作用:
            修改存储中的步骤状态。
        """

        return self._task_store.update_step_status(
            step_id,
            status,
            output_summary=output_summary,
            error=error,
        )

    async def stream_model(self, messages: List[RuntimeMessage]) -> AsyncIterator[ModelDelta]:
        """为工作流消息流式产出模型增量。

        参数:
            messages: 发送给所配置模型的运行时消息。

        生成:
            来自所配置适配器的模型增量。

        异常:
            ValueError: 如果所配置的上下文预算非法。
            RuntimeError: 如果上下文超出预算，或模型适配器失败。

        副作用:
            在上下文校验之后通过模型适配器执行服务商 I/O。
        """

        validate_context_budget(messages, self.settings.max_context_chars)
        model_tools = build_model_tool_definitions(self._list_agent_visible_tools())
        async for delta in self._model_adapter.stream(messages, model_tools):
            yield delta

    def execute_tool(self, call: ToolCall, step_id: Optional[str] = None) -> ToolObservation:
        """通过调度器执行一个模型请求的工具调用。

        参数:
            call: 模型请求的工具调用。
            step_id: 可选工具步骤标识，用于持久化关联。

        返回:
            归一化的工具观测结果。

        异常:
            无。调度器错误会作为观测结果返回。

        副作用:
            可能通过 Tool v2 Runtime 或兼容调度器执行工具副作用。
        """

        denied_tool = self._find_agent_denied_model_visible_tool(call.tool_name)
        if denied_tool is not None:
            self._logger.warning(
                "agent_tool_denied",
                extra={
                    "msg": f"Agent 档案不允许调用工具 {denied_tool.name}，已拒绝",
                    "data": {
                        "agent_id": self._agent_profile.agent_id,
                        "tool_name": denied_tool.name,
                        "permission": denied_tool.permission,
                    },
                },
            )
            return ToolObservation(
                tool_name=denied_tool.name,
                status="error",
                content="",
                error=f"agent does not allow tool: {denied_tool.name}",
                permission=denied_tool.permission,
                approval_status="agent_denied",
            )
        if self._tool_runtime is not None:
            return self._tool_runtime.execute_single_tool_call(
                call,
                ToolExecutionContext(run_id=self._tool_run_id, step_id=step_id),
            )
        return self._tool_scheduler.execute(call)

    def execute_tools(self, calls: List[ToolCall], step_id: Optional[str] = None) -> List[ToolObservation]:
        """通过 Tool v2 Runtime 执行同一模型响应中的多个工具调用。

        参数:
            calls: 同一模型响应请求的工具调用。
            step_id: 关联模型步骤标识。

        返回:
            与输入调用顺序一致的观测结果。

        异常:
            RuntimeError: 当旧兼容调度器收到多个调用时抛出。

        副作用:
            可能并发执行工具副作用。
        """

        if self._tool_runtime is not None:
            return self._tool_runtime.execute_tool_calls(
                calls, ToolExecutionContext(run_id=self._tool_run_id, step_id=step_id)
            )
        if len(calls) != 1:
            raise RuntimeError("multiple tool calls require ToolRuntime")
        return [self.execute_tool(calls[0], step_id)]

    def _list_agent_visible_tools(self) -> List[ToolDefinition]:
        """返回当前 Agent 档案允许的对模型可见的工具。

        参数:
            无。

        返回:
            同时被调度器策略与 Agent 档案可见的工具定义。

        异常:
            无。

        副作用:
            无。
        """

        return [
            tool
            for tool in self._list_platform_visible_tools()
            if self._agent_profile.allows_tool(tool.name, tool.permission)
        ]

    def _find_agent_denied_model_visible_tool(self, tool_name: str) -> Optional[ToolDefinition]:
        """返回被 Agent 档案拒绝、但调度器可见的工具。

        参数:
            tool_name: 模型请求的工具名。

        返回:
            当调度器暴露该工具但 Agent 档案不允许它时，返回匹配的工具定义，
            否则返回 ``None``。

        异常:
            无。

        副作用:
            无。
        """

        for tool in self._list_platform_visible_tools():
            if tool.name == tool_name and not self._agent_profile.allows_tool(
                tool.name,
                tool.permission,
            ):
                return tool
        return None

    def _list_platform_visible_tools(self) -> List[ToolDefinition]:
        """返回当前工具执行入口允许模型看到的工具定义。

        参数:
            无。

        返回:
            配置 Tool v2 Runtime 时返回其可见工具，否则返回兼容调度器结果。

        异常:
            无。

        副作用:
            无。
        """

        if self._tool_runtime is not None:
            return self._tool_runtime.list_model_visible_tools()
        return self._tool_scheduler.list_model_visible_tools()

    def has_task_status(self, task_id: str, status: str) -> bool:
        """返回任务当前是否具有某状态。

        参数:
            task_id: 需要检查的任务标识符。
            status: 需要比较的状态值。

        返回:
            当任务当前具有所请求的状态时为 True。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            无。
        """

        return self._task_store.has_status(task_id, status)

    def update_task_status(self, task_id: str, status: str) -> TaskRecord:
        """通过运行时拥有的存储更新任务状态。

        参数:
            task_id: 需要修改的任务标识符。
            status: 新的任务状态。

        返回:
            已更新的任务记录。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            修改存储中的任务状态。
        """

        return self._task_store.update_status(task_id, status)

    def close_running_steps(self, task_id: str, status: str, error: str) -> int:
        """关闭任务的运行中步骤。

        参数:
            task_id: 需要关闭其运行中步骤的任务标识符。
            status: 应用于运行中步骤的终态状态。
            error: 写入被更新步骤的错误或终态原因。

        返回:
            被更新的步骤行数。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            修改存储中运行中的步骤状态。
        """

        return self._task_store.close_running_steps_for_task(task_id, status, error)

    def record_event(self, event_type: EventType, task_id: str, payload: dict) -> RuntimeEvent:
        """持久化并返回一个运行时事件。

        参数:
            event_type: 稳定的运行时事件类型枚举成员。
            task_id: 与该事件关联的任务标识符。
            payload: 可序列化为 JSON 的事件载荷。

        返回:
            已持久化的运行时事件。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            追加一个事件并写入一条运行时日志。
        """

        return self._record_event(event_type, task_id, payload)

    def create_checkpoint(self, task_id: str, stage: str) -> RuntimeEvent:
        """创建一个状态级检查点并返回其运行时事件。

        参数:
            task_id: 需要对其状态做快照的任务标识符。
            stage: 正在产出该检查点的运行时阶段。

        返回:
            持久化成功时返回 ``checkpoint_created`` 事件，否则返回带有诊断错误的
            ``checkpoint_failed`` 事件。

        异常:
            KeyError: 如果因任务不存在导致事件记录失败。

        副作用:
            读取任务、步骤和事件状态；写入检查点状态；写入日志；
            追加一个运行时事件。
        """

        try:
            task = self._task_store.get_task(task_id)
            steps = self._task_store.list_steps_for_task(task_id)
            events = self._task_store.list_events(task_id)
            checkpoint_draft = build_checkpoint_snapshot(
                task,
                steps,
                events,
                stage,
                self._agent_profile,
            )
            checkpoint = self._task_store.create_checkpoint(
                task_id=task_id,
                stage=stage,
                summary=checkpoint_draft.summary,
                snapshot=checkpoint_draft.snapshot,
            )
        except Exception as exc:
            self._logger.exception(
                "checkpoint_failed",
                extra={
                    "msg": f"创建状态检查点失败，task_id={task_id}，stage={stage}",
                    "data": {"task_id": task_id, "stage": stage},
                },
            )
            return self._record_checkpoint_failure(task_id, stage, exc)

        self._logger.info(
            "checkpoint_created",
            extra={
                "msg": f"状态检查点已创建，task_id={task_id}，stage={stage}",
                "data": {
                    "task_id": task_id,
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "stage": stage,
                },
            },
        )
        return self.record_event(
            EventType.CHECKPOINT_CREATED,
            task_id,
            {
                "checkpoint_id": checkpoint.checkpoint_id,
                "stage": checkpoint.stage,
                "summary": checkpoint.summary,
            },
        )

    def _record_checkpoint_failure(
        self,
        task_id: str,
        stage: str,
        checkpoint_error: Exception,
    ) -> RuntimeEvent:
        """记录或合成一个检查点失败事件。

        参数:
            task_id: 与失败检查点关联的任务标识符。
            stage: 未能创建检查点的运行时阶段。
            checkpoint_error: 原始的检查点持久化错误。

        返回:
            尽可能持久化的 ``checkpoint_failed`` 事件；否则返回一个适合当前
            运行时流、但未被持久化的事件。

        异常:
            无。

        副作用:
            尝试追加一个运行时事件；如果事件持久化也失败，则写入一条额外的错误日志。
        """

        payload = {"stage": stage, "error": str(checkpoint_error), "event_persisted": True}
        try:
            return self.record_event(EventType.CHECKPOINT_FAILED, task_id, payload)
        except Exception as event_error:
            self._logger.exception(
                "checkpoint_failed_event_unpersisted",
                extra={
                    "msg": f"检查点失败事件也未能持久化，task_id={task_id}，stage={stage}",
                    "data": {"task_id": task_id, "stage": stage},
                },
            )
            return RuntimeEvent(
                event_type=EventType.CHECKPOINT_FAILED,
                task_id=task_id,
                payload={
                    "stage": stage,
                    "error": str(checkpoint_error),
                    "event_persisted": False,
                    "event_error": str(event_error),
                },
            )

    def log_exception(self, event_name: str, extra: dict | None = None) -> None:
        """通过运行时日志记录器写入异常诊断信息。

        参数:
            event_name: 稳定日志事件名。
            extra: 需要写入日志的字段，应遵循规范用 ``msg``/``data`` 组织。

        返回:
            无。

        异常:
            无。

        副作用:
            使用配置好的日志记录器写入一条异常日志。
        """

        self._logger.exception(event_name, extra=extra or {})
