"""业务 ReAct 循环：单一执行者、显式恢复入口；持久进度归 Run。"""

from __future__ import annotations

import asyncio

from app.config.configuration import get_tool_system
from app.core.llm_provider.model_factory import ResolvedChatModel, resolve_chat_model
from app.core.runtime.execution_mode import ExecutionMode
from app.core.tools.schemas import UserDecision, UserDecisionKind
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.execution_state.react_route import ReactRoute
from app.core.workflow.execution_state.runtime_context import RuntimeContext
from app.core.workflow.node_helper.common import RunStopped, execution_scope
from app.core.workflow.nodes import (
    _model_node,
    _observe_node,
    _structured_output_node,
    _tools_node,
)
from app.core.workflow.nodes.wait_user_node import suspend_for_input, wait_user_node
from app.core.workflow.workflow_operations import WorkflowOperations
from app.models.conversation_run_progress import ConversationRunProgress
from app.service.conversation_run.task_input_service import TaskInputService
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class ReactLikeWorkflow:
    """仅实现项目需要的 ReAct 顺序，不提供图重放或任意节点恢复。"""

    async def run(
        self, operations: WorkflowOperations, execution_mode: ExecutionMode = "fresh"
    ) -> None:
        resolved = resolve_chat_model(
            agent_profile=operations.agent_profile, tools=operations.task_tool_schemas
        )
        try:
            await self._drive(operations, resolved, execution_mode)
        except RunStopped:
            return
        finally:
            await resolved.model.close()

    async def finalize_run(self, run_id: int) -> None:
        """在执行器完成 Run 状态收敛后执行工作流资源收尾（当前无额外动作）。"""

        return None

    async def _drive(
        self,
        operations: WorkflowOperations,
        resolved: ResolvedChatModel,
        execution_mode: ExecutionMode,
    ) -> None:
        """等待决定从 Run 读取；HTTP 参数不构成另一份恢复事实。"""
        run = operations.get_current_run()
        profile = operations.agent_profile
        context = task_runtime_spaces.get_or_create(run.task_id).get_context_manager(
            agent_profile=profile
        )
        context.begin_run(run)
        # 每次显式执行获得新预算；累计步骤号持续递增，避免恢复后复用流式 part 身份。
        progress = run.execution_data
        runtime = RuntimeContext(
            operations=operations,
            run=run,
            model=resolved.model,
            max_steps=progress.step_count + profile.max_steps,
            structured_output=profile.structured_output,
            thinking_channel="reasoning_content" if resolved.supports_thinking else "",
            usage_stats=(
                ConversationRunUsageStats(**run.usage)
                if run.usage is not None
                else ConversationRunUsageStats()
            ),
            user_input_decision_handlers=get_tool_system().user_input_decision_handlers,
        )
        state = ReactExecutionState(
            step_count=progress.step_count,
            tool_error_count=progress.tool_error_count,
            next_node=ReactRoute.MODEL,
            last_tool_results={},
        )
        with execution_scope(runtime, context):
            if execution_mode == "resume_with_input":
                decision = progress.decision
                waiting = progress.waiting
                if decision is None or waiting is None:
                    raise ValueError("等待恢复缺少已提交的请求或决定")
                runtime.step_context.projected_tool_calls.update(waiting.projected_tool_calls)
                state = ReactExecutionState.model_validate(
                    {
                        **state.model_dump(),
                        "instruction": waiting.instruction,
                        "last_tool_results": waiting.last_tool_results.model_dump(),
                        "calls": [waiting.pending_call],
                    }
                )
                state = state.model_copy(update=wait_user_node(state, decision))
                self._save_progress(operations, state)
            while True:
                if state.next_node == ReactRoute.MODEL:
                    if operations.is_current_run_cancelled():
                        operations.cancel_run_if_running(usage_stats=runtime.usage_stats)
                        return
                    if state.step_count < runtime.max_steps:
                        await asyncio.to_thread(
                            TaskInputService().consume_into_context, context, run.id
                        )
                    # 请求前持久化已占用的步骤号，取消后不会复用草稿/part 身份。
                    operations.save_execution(
                        ConversationRunProgress(
                            step_count=state.step_count + 1,
                            tool_error_count=state.tool_error_count,
                        )
                    )
                    patch = await _model_node(state)
                elif state.next_node == ReactRoute.TOOLS:
                    patch = await _tools_node(state)
                elif state.next_node == ReactRoute.OBSERVE:
                    patch = await _observe_node(state)
                elif state.next_node == ReactRoute.WAIT_USER:
                    if operations.is_current_run_cancelled():
                        patch = wait_user_node(
                            state, UserDecision(kind=UserDecisionKind.FORCE_REJECT)
                        )
                    else:
                        suspend_for_input(state)
                        return
                elif state.next_node == ReactRoute.STRUCTURED_OUTPUT:
                    patch = await _structured_output_node(state)
                elif state.next_node == ReactRoute.END:
                    # 主线由 model 落 completed；structured 只改结果字段。其余失败节点就地落定，
                    # 执行态不携带待落库的最终回答；loop 到 END 只停止驱动。
                    return
                else:
                    raise ValueError(f"未知执行步骤：{state.next_node}")
                state = state.model_copy(update=patch)
                self._save_progress(operations, state)

    @staticmethod
    def _save_progress(operations: WorkflowOperations, state: ReactExecutionState) -> None:
        operations.save_execution(
            ConversationRunProgress(
                step_count=state.step_count, tool_error_count=state.tool_error_count
            )
        )
