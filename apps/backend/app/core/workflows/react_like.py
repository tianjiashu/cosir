"""默认的、纯文本 ReAct 风格工作流策略。"""

from dataclasses import dataclass
import json
from typing import AsyncIterator, List, Optional

from app.events.types import EventType, RuntimeEvent
from app.models.base import RuntimeMessage
from app.core.runtime.operations import RuntimeOperations
from app.storage.records import StepRecord, TaskRecord
from app.tools.types import ToolCall, ToolObservation
from app.core.workflows.step_controller import LangGraphStepController


@dataclass
class ReactLikeState:
    """追踪一次 ReAct 风格工作流运行的、可变的运行状态。

    参数:
        messages: 为模型调用累积的运行时消息。
        step_count: 已经尝试过的模型步骤数。
        tool_error_count: 本次运行看到的连续工具错误数。
        requested_tool: 当前模型步骤是否请求了工具。
        final_response: 当前模型步骤是否产生了最终响应。
        terminal: 工作流是否到达了终态。

    返回:
        一次工作流运行的可变状态值。

    异常:
        无。

    副作用:
        无。
    """

    messages: List[RuntimeMessage]
    step_count: int = 0
    tool_error_count: int = 0
    requested_tool: bool = False
    final_response: bool = False
    terminal: bool = False


class ReactLikeWorkflow:
    """运行第一个纯文本的 ReAct 风格工作流。"""

    def __init__(self, step_controller: Optional[LangGraphStepController] = None) -> None:
        """初始化工作流控制依赖项。

        参数:
            step_controller: 用于工作流步骤决定的可选控制器。

        返回:
            无。

        异常:
            无。

        副作用:
            在可用时编译默认的 LangGraph 步骤控制器。
        """

        self._step_controller = step_controller or LangGraphStepController()

    async def run(
        self,
        task: TaskRecord,
        operations: RuntimeOperations,
    ) -> AsyncIterator[RuntimeEvent]:
        """通过默认的 ReAct 风格策略运行一个任务。

        参数:
            task: 由运行时选中的任务记录。
            operations: 面向状态、事件、模型和工具的、运行时拥有的操作门面。

        生成:
            描述模型增量、工具活动与终态的运行时事件。

        异常:
            RuntimeError: 如果模型流在没有工具调用或最终输出的情况下结束。

        副作用:
            更新任务/步骤状态、调用模型适配器、通过调度器执行工具，并记录运行时事件。
        """

        turn = operations.get_turn_for_task(task.task_id)
        state = ReactLikeState(messages=operations.build_messages(task))

        while True:
            decision = await self._step_controller.next_step(
                step_count=state.step_count,
                max_steps=operations.settings.max_steps,
            )
            state.step_count = decision.step_count
            if not decision.can_continue:
                break

            model_step = operations.create_step(
                turn_id=turn.turn_id,
                step_type="model_call",
                status="running",
                input_summary=f"model step {state.step_count}",
            )
            yield operations.record_event(
                EventType.STEP_STARTED,
                task.task_id,
                {"step_type": "model_call", "step_index": state.step_count},
            )

            async for event in self._run_model_step(task, operations, model_step, state):
                yield event

            if state.terminal:
                return
            if state.requested_tool:
                continue
            if state.final_response:
                return

            operations.update_step(
                model_step.step_id,
                "failed",
                error="invalid_model_output",
            )
            yield operations.create_checkpoint(task.task_id, "invalid_model_output")
            raise RuntimeError("invalid_model_output")

        operations.log_exception(
            "task_failed",
            extra={
                "task_id": task.task_id,
                "reason": "max_steps_reached",
                "max_steps": operations.settings.max_steps,
            },
        )
        operations.update_task_status(task.task_id, "failed")
        operations.close_running_steps(task.task_id, "failed", "max_steps_reached")
        yield operations.create_checkpoint(task.task_id, "max_steps_reached")
        yield operations.record_event(
            EventType.RUN_FAILED,
            task.task_id,
            {"status": "failed", "error": "max_steps_reached"},
        )

    async def _run_model_step(
        self,
        task: TaskRecord,
        operations: RuntimeOperations,
        model_step: StepRecord,
        state: ReactLikeState,
    ) -> AsyncIterator[RuntimeEvent]:
        """运行一个模型步骤并分发模型增量。

        参数:
            task: 正在执行的任务。
            operations: 运行时操作门面。
            model_step: 本次迭代的、持久化的模型步骤记录。
            state: 需要更新的可变工作流状态。

        生成:
            由模型输出、工具调用或最终响应产生的运行时事件。

        异常:
            RuntimeError: 如果模型适配器在流式传输时失败。

        副作用:
            流式传输模型输出，并可能执行工具或完成任务。
        """

        state.requested_tool = False
        state.final_response = False
        tool_calls: List[ToolCall] = []
        yield operations.record_event(
            EventType.MODEL_REQUESTED,
            task.task_id,
            {"step_id": model_step.step_id, "step_index": state.step_count},
        )

        async for delta in operations.stream_model(state.messages):
            if operations.has_task_status(task.task_id, "cancelled"):
                operations.close_running_steps(task.task_id, "cancelled", "run_cancelled")
                yield operations.record_event(
                    EventType.RUN_CANCELLED,
                    task.task_id,
                    {"status": "cancelled"},
                )
                state.terminal = True
                return

            if delta.text:
                yield operations.record_event(
                    EventType.MODEL_OUTPUT_DELTA,
                    task.task_id,
                    {"delta": delta.text},
                )

            if delta.tool_call is not None:
                state.requested_tool = True
                tool_calls.append(delta.tool_call)
                continue

            if delta.is_final:
                yield operations.record_event(
                    EventType.MODEL_COMPLETED,
                    task.task_id,
                    {"step_id": model_step.step_id, "step_index": state.step_count},
                )
                if tool_calls:
                    async for event in self._handle_tool_calls(
                        task, operations, model_step, tool_calls, state
                    ):
                        yield event
                    return
                state.final_response = True
                async for event in self._handle_final_response(task, operations, model_step):
                    yield event
                state.terminal = True
                return

        if tool_calls:
            yield operations.record_event(
                EventType.MODEL_COMPLETED,
                task.task_id,
                {"step_id": model_step.step_id, "step_index": state.step_count},
            )
            async for event in self._handle_tool_calls(
                task, operations, model_step, tool_calls, state
            ):
                yield event

    async def _handle_tool_calls(
        self,
        task: TaskRecord,
        operations: RuntimeOperations,
        model_step: StepRecord,
        tool_calls: List[ToolCall],
        state: ReactLikeState,
    ) -> AsyncIterator[RuntimeEvent]:
        """执行同一模型响应中的一组工具调用并保留其输入顺序。

        参数:
            task: 正在运行的任务。
            operations: Runtime 工具执行门面。
            model_step: 产生本组调用的模型步骤。
            tool_calls: 已聚合的模型工具调用。
            state: 需要回填观测消息的工作流状态。

        生成:
            工具请求、开始、完成、审批和观测事件。

        异常:
            无。单调用错误由 Tool Runtime 归一化。

        副作用:
            创建工具步骤、调用并发工具执行链并更新任务状态。
        """

        operations.update_step(model_step.step_id, "completed", output_summary="tool_calls")
        yield operations.create_checkpoint(task.task_id, "model_tool_calls_requested")
        observations = operations.execute_tools(tool_calls, model_step.step_id)
        tool_group_id = f"{model_step.step_id}:tools"
        for index, (call, observation) in enumerate(zip(tool_calls, observations)):
            tool_call_id = observation.tool_call_id or call.call_id
            yield operations.record_event(
                EventType.TOOL_CALL_REQUESTED,
                task.task_id,
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": call.tool_name,
                    "arguments": call.arguments,
                    "tool_group_id": tool_group_id,
                    "tool_call_index": index,
                },
            )
            yield operations.record_event(
                EventType.TOOL_CALL_STARTED,
                task.task_id,
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": call.tool_name,
                    "tool_group_id": tool_group_id,
                    "tool_call_index": index,
                },
            )
        for index, (call, observation) in enumerate(zip(tool_calls, observations)):
            tool_call_id = observation.tool_call_id or call.call_id
            yield operations.record_event(
                EventType.TOOL_CALL_FINISHED,
                task.task_id,
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": observation.tool_name,
                    "status": observation.status,
                    "error": observation.error,
                    "artifact_id": observation.artifact_id,
                    "tool_group_id": tool_group_id,
                    "tool_call_index": index,
                },
            )
            if observation.status == "approval_required":
                yield operations.record_event(
                    EventType.TOOL_APPROVAL_REQUIRED,
                    task.task_id,
                    {
                        "tool_call_id": tool_call_id,
                        "tool_name": observation.tool_name,
                        "permission": observation.permission,
                        "approval_status": observation.approval_status,
                        "reason": observation.error,
                        "tool_group_id": tool_group_id,
                        "tool_call_index": index,
                    },
                )
                operations.update_task_status(task.task_id, "waiting")
                yield operations.create_checkpoint(task.task_id, "tool_approval_required")
                state.terminal = True
                return
            if self._tool_error_limit_reached(observation, state, operations):
                operations.update_task_status(task.task_id, "failed")
                operations.close_running_steps(task.task_id, "failed", "tool_error_limit_reached")
                yield operations.create_checkpoint(task.task_id, "tool_error_limit_reached")
                yield operations.record_event(
                    EventType.RUN_FAILED,
                    task.task_id,
                    {"status": "failed", "error": "tool_error_limit_reached", "tool_name": observation.tool_name},
                )
                state.terminal = True
                return
            self._append_tool_exchange(state, model_step, call, observation)
            yield operations.record_event(EventType.OBSERVATION_ADDED, task.task_id, {"tool_name": observation.tool_name, "status": observation.status})

    async def _handle_tool_call(
        self,
        task: TaskRecord,
        operations: RuntimeOperations,
        model_step: StepRecord,
        tool_call: ToolCall,
        state: ReactLikeState,
    ) -> AsyncIterator[RuntimeEvent]:
        """执行模型请求的一个工具调用。

        参数:
            task: 正在执行的任务。
            operations: 运行时操作门面。
            model_step: 产生该工具调用的模型步骤。
            tool_call: 模型请求的工具调用。
            state: 需要用观测结果更新的可变工作流状态。

        生成:
            用于工具请求、执行与观测结果的运行时事件。

        异常:
            无。工具失败会被归一化为观测结果。

        副作用:
            通过 RuntimeOperations 执行一个工具并更新持久化的步骤。
        """

        operations.update_step(
            model_step.step_id,
            "completed",
            output_summary=f"tool_call:{tool_call.tool_name}",
        )
        yield operations.create_checkpoint(task.task_id, "model_tool_call_requested")

        tool_step = operations.create_step(
            turn_id=model_step.turn_id,
            step_type="tool_call",
            status="running",
            input_summary=tool_call.tool_name,
        )

        observation = operations.execute_tool(tool_call, tool_step.step_id)
        tool_call_id = observation.tool_call_id or tool_call.call_id
        tool_group_id = f"{model_step.step_id}:tools"
        yield operations.record_event(
            EventType.TOOL_CALL_REQUESTED,
            task.task_id,
            {
                "tool_call_id": tool_call_id,
                "tool_name": tool_call.tool_name,
                "arguments": tool_call.arguments,
                "tool_group_id": tool_group_id,
                "tool_call_index": 0,
            },
        )
        yield operations.record_event(
            EventType.TOOL_CALL_STARTED,
            task.task_id,
            {
                "tool_call_id": tool_call_id,
                "tool_name": tool_call.tool_name,
                "tool_group_id": tool_group_id,
                "tool_call_index": 0,
            },
        )
        operations.update_step(
            tool_step.step_id,
            "completed" if observation.status == "success" else "failed",
            output_summary=observation.content[:200],
            error=observation.error or None,
        )
        yield operations.record_event(
            EventType.TOOL_CALL_FINISHED,
            task.task_id,
            {
                "tool_name": observation.tool_name,
                "tool_call_id": tool_call_id,
                "status": observation.status,
                "error": observation.error,
                "artifact_id": observation.artifact_id,
                "tool_group_id": tool_group_id,
                "tool_call_index": 0,
            },
        )
        yield operations.create_checkpoint(task.task_id, "tool_call_finished")

        if observation.status == "approval_required":
            yield operations.record_event(
                EventType.TOOL_APPROVAL_REQUIRED,
                task.task_id,
                {
                    "tool_name": observation.tool_name,
                    "tool_call_id": tool_call_id,
                    "permission": observation.permission,
                    "approval_status": observation.approval_status,
                    "reason": observation.error,
                    "tool_group_id": tool_group_id,
                    "tool_call_index": 0,
                },
            )
            operations.log_exception(
                "task_failed",
                extra={
                    "task_id": task.task_id,
                    "reason": "tool_approval_required",
                    "tool_name": observation.tool_name,
                    "permission": observation.permission,
                },
            )
            operations.update_task_status(task.task_id, "failed")
            operations.close_running_steps(task.task_id, "failed", "tool_approval_required")
            yield operations.create_checkpoint(task.task_id, "tool_approval_required")
            yield operations.record_event(
                EventType.RUN_FAILED,
                task.task_id,
                {
                    "status": "failed",
                    "error": "tool_approval_required",
                    "tool_name": observation.tool_name,
                    "permission": observation.permission,
                },
            )
            state.terminal = True
            return

        if self._tool_error_limit_reached(observation, state, operations):
            operations.log_exception(
                "task_failed",
                extra={
                    "task_id": task.task_id,
                    "reason": "tool_error_limit_reached",
                    "tool_name": observation.tool_name,
                    "limit": operations.settings.tool_error_limit,
                },
            )
            operations.update_task_status(task.task_id, "failed")
            operations.close_running_steps(task.task_id, "failed", "tool_error_limit_reached")
            yield operations.create_checkpoint(task.task_id, "tool_error_limit_reached")
            yield operations.record_event(
                EventType.RUN_FAILED,
                task.task_id,
                {
                    "status": "failed",
                    "error": "tool_error_limit_reached",
                    "tool_name": observation.tool_name,
                },
            )
            state.terminal = True
            return

        self._append_tool_exchange(state, model_step, tool_call, observation)
        yield operations.record_event(
            EventType.OBSERVATION_ADDED,
            task.task_id,
            {"tool_name": observation.tool_name, "status": observation.status},
        )

    async def _handle_final_response(
        self,
        task: TaskRecord,
        operations: RuntimeOperations,
        model_step: StepRecord,
    ) -> AsyncIterator[RuntimeEvent]:
        """在模型发出最终响应之后完成任务。

        参数:
            task: 正在执行的任务。
            operations: 运行时操作门面。
            model_step: 发出最终响应的模型步骤。

        生成:
            最终响应与运行结束事件。

        异常:
            KeyError: 如果任务或步骤状态无法被更新。

        副作用:
            将模型步骤与任务标记为已完成。
        """

        operations.update_step(
            model_step.step_id,
            "completed",
            output_summary="final_response",
        )
        yield operations.record_event(
            EventType.FINAL_RESPONSE,
            task.task_id,
            {"status": "completed"},
        )
        operations.update_task_status(task.task_id, "completed")
        yield operations.create_checkpoint(task.task_id, "run_finished")
        yield operations.record_event(
            EventType.RUN_FINISHED,
            task.task_id,
            {"status": "completed"},
        )

    def _tool_error_limit_reached(
        self,
        observation: ToolObservation,
        state: ReactLikeState,
        operations: RuntimeOperations,
    ) -> bool:
        """更新工具错误计数并返回是否达到上限。

        参数:
            observation: 由调度器返回的工具观测结果。
            state: 包含当前错误计数的可变工作流状态。
            operations: 包含所配置限制的运行时操作门面。

        返回:
            当连续工具错误达到所配置的上限时为 True。

        异常:
            无。

        副作用:
            修改 ``state.tool_error_count``。
        """

        if observation.status == "error":
            state.tool_error_count += 1
        else:
            state.tool_error_count = 0
        return state.tool_error_count >= operations.settings.tool_error_limit

    def _append_tool_exchange(
        self,
        state: ReactLikeState,
        model_step: StepRecord,
        tool_call: ToolCall,
        observation: ToolObservation,
    ) -> None:
        """追加助手工具调用与工具观测消息。

        参数:
            state: 包含模型消息的可变工作流状态。
            model_step: 产生该工具调用的模型步骤。
            tool_call: 模型请求的工具调用。
            observation: 需要加入模型上下文的工具观测结果。

        返回:
            无。

        异常:
            无。

        副作用:
            向 ``state.messages`` 追加助手与工具消息。
        """

        tool_call_id = tool_call.call_id or f"local_{model_step.step_id}"
        arguments_json = json.dumps(tool_call.arguments, ensure_ascii=False, sort_keys=True)
        state.messages.append(
            RuntimeMessage(
                role="assistant",
                content_text="",
                metadata={
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_call.tool_name,
                    "tool_arguments_json": arguments_json,
                },
            )
        )
        state.messages.append(
            RuntimeMessage(
                role="tool",
                content_text=observation.content or observation.error,
                metadata={
                    "tool_call_id": tool_call_id,
                    "tool_name": observation.tool_name,
                    "status": observation.status,
                },
            )
        )
