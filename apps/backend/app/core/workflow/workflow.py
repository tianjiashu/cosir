"""业务 ReAct 循环：按 ``ReactRoute`` 流转的节点注册表；单一执行者、两个恢复点；持久进度归 Run。"""

from __future__ import annotations

from app.core.llm_provider.model_factory import resolve_chat_model
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
from app.core.workflow.nodes.wait_user_node import wait_user_node
from app.core.workflow.tool_continuation import restore_tool_batch
from app.models.conversation_run_progress import ConversationRunProgress
from app.service import depends as service_depends


class ReactLikeWorkflow:
    """按 ``ReactRoute`` 流转的节点循环；不提供图重放或任意节点恢复。

    扩展方式：``ReactRoute`` 新增成员，在 ``__init__`` 的节点表里登记对应节点，由上游节点返回
    该 route 流入。
    恢复点只有两个：等待恢复从 ``wait_user`` 进入，其余（新 Run、续跑、编辑重跑、交接）都从
    ``model`` 进入。新增节点必须能在从 ``model`` 重新进入后安全重跑，或在下一次 ``model`` 前完成。
    """

    def __init__(self) -> None:
        self._nodes = {
            ReactRoute.MODEL: _model_node,
            ReactRoute.TOOLS: _tools_node,
            ReactRoute.OBSERVE: _observe_node,
            ReactRoute.WAIT_USER: wait_user_node,
            ReactRoute.STRUCTURED_OUTPUT: _structured_output_node,
        }

    async def run(self, rc: RuntimeContext) -> None:
        """驱动一次 Run：解析本次独占的模型、按持久化进度补齐驱动期状态，再循环节点直到 END。

        ``rc`` 由装配方注入本次 Run 的事实与协作者；模型 schema 用 Task 固化的完整工具 schema，
        不按本轮准入过滤，避免同一 Task 的请求前缀变化。
        """
        resolved = resolve_chat_model(
            agent_profile=rc.profile, tools=list(rc.task_space.task_tool_schemas)
        )
        rc.model = resolved.model
        rc.thinking_channel = "reasoning_content" if resolved.supports_thinking else ""
        try:
            await self._drive(rc)
        except RunStopped:
            return
        finally:
            await resolved.model.close()

    async def _drive(self, rc: RuntimeContext) -> None:
        """入口只由 Run 的持久化进度决定，调用方不传恢复模式。

        进度里有已绑定决定的待决调用（等待态被决定唤醒）时从 ``wait_user`` 进入，其余（新 Run、
        续跑、编辑重跑、交接）都从 ``model`` 进入。待决调用只在挂起时写入、决定消费后的第一次
        进度保存即清空，所以它不会让后续执行误入 ``wait_user``。
        """
        context = rc.task_space.get_context_manager(agent_profile=rc.profile)
        context.begin_run(rc.run)
        progress = rc.run.execution_data
        # 每次显式执行获得新预算；累计步骤号持续递增，避免恢复后复用流式 part 身份。
        rc.step_limit = progress.step_count + rc.profile.max_steps
        if rc.run.usage is not None:
            rc.usage_stats = ConversationRunUsageStats(**rc.run.usage)
        entry = ReactRoute.MODEL
        continuation = progress.continuation
        if continuation is not None and continuation.decision is not None:
            rc.batch = restore_tool_batch(continuation)
            rc.resume_decision = continuation.decision
            entry = ReactRoute.WAIT_USER
        state = ReactExecutionState(
            step_count=progress.step_count,
            tool_error_count=progress.tool_error_count,
            next_node=entry,
        )
        with execution_scope(rc, context):
            # 终态由产出节点就地落定，循环到 END 只停止驱动。
            while state.next_node != ReactRoute.END:
                patch = await self._nodes[state.next_node](state)
                state = state.model_copy(update=patch)
                service_depends.get_conversation_run_state_service().save_execution(
                    rc.run.id,
                    ConversationRunProgress(
                        step_count=state.step_count, tool_error_count=state.tool_error_count
                    ),
                )
