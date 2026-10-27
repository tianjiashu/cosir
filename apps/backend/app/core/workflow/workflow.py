"""业务 ReAct 循环：按 ``ReactRoute`` 流转的节点注册表；单一执行者、单一入口；持久进度归 Run。"""

from __future__ import annotations

from app.core.llm_provider.model_factory import resolve_chat_model
from app.core.workflow.branch import task_title
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.models.enums.react_route import ReactRoute
from app.core.workflow.execution_state.runtime_context import RuntimeContext
from app.core.workflow.node_helper.common import RunStopped, execution_scope
from app.core.workflow.nodes.model_node import _model_node
from app.core.workflow.nodes.observation_node import _observe_node
from app.core.workflow.nodes.structured_output_node import _structured_output_node
from app.core.workflow.nodes.tools_node import _tools_node
from app.core.workflow.nodes.prepare_input_node import _prepare_input_node
from app.service import depends as service_depends


class ReactLikeWorkflow:
    """按 ``ReactRoute`` 流转的节点循环；不提供图重放或任意节点恢复。

    扩展方式：``ReactRoute`` 新增成员，在 ``__init__`` 的节点表里登记对应节点，由上游节点返回
    该 route 流入。
    入口只有 ``prepare_input``：新 Run、续跑、唤醒、编辑重跑、交接都从它进入；它也是每次请求模型前
    的唯一关口，依次判定取消、挂起、步数上限，再并入 mailbox 中的消息后流向 ``model``。挂起时工具调用已在本步闭合，Run 进度不冻结任何调用。新增节点必须能在
    重新进入后安全重跑，或在下一次模型请求前完成。
    """

    def __init__(self) -> None:
        self._nodes = {
            ReactRoute.PREPARE_INPUT: _prepare_input_node,
            ReactRoute.MODEL: _model_node,
            ReactRoute.TOOLS: _tools_node,
            ReactRoute.OBSERVE: _observe_node,
            ReactRoute.STRUCTURED_OUTPUT: _structured_output_node,
        }

    async def run(self, rc: RuntimeContext) -> None:
        """驱动一次 Run：解析本次独占的模型、按持久化进度补齐驱动期状态，再循环节点直到 END。

        ``rc`` 由装配方注入本次 Run 的事实与协作者；模型 schema 用 Task 固化的完整工具 schema，
        不按本轮准入过滤，避免同一 Task 的请求前缀变化。client 归本次驱动独占，标题支线也复用它，
        所以关闭前先收尾支线（见 ``branch.task_title.settle``）。
        """
        resolved = resolve_chat_model(
            model=rc.agent.model, tools=list(rc.task_space.task_tool_schemas)
        )
        rc.model = resolved.model
        rc.thinking_channel = "reasoning_content" if resolved.supports_thinking else ""
        try:
            await self._drive(rc)
        except RunStopped:
            return
        finally:
            # 标题支线复用这个 client：先收尾支线再关闭。
            try:
                await task_title.settle(rc)
            finally:
                await resolved.model.close()

    async def _drive(self, rc: RuntimeContext) -> None:
        """从 ``prepare_input`` 进入，循环节点直到 END；调用方不传恢复模式。"""
        context = rc.task_space.get_context_manager(agent=rc.agent)
        context.begin_run(rc.run)
        # 整个执行状态都落库，但新驱动只延续跨驱动事实，总从 prepare_input 进入（见 for_new_drive）。
        state = rc.run.execution_data.for_new_drive(rc.agent.max_steps)
        if rc.run.usage is not None:
            rc.usage_stats = ConversationRunUsageStats(**rc.run.usage)
        with execution_scope(rc, context):
            # 终态由产出节点就地落定，循环到 END 只停止驱动。
            while state.next_node != ReactRoute.END:
                patch = await self._nodes[state.next_node](state)
                state = state.model_copy(update=patch)
                service_depends.get_conversation_run_ledger().save_execution(
                    rc.run.id, state
                )
