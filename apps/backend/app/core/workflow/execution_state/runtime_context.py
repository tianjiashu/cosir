"""一次 Run 驱动的 Run / Task 级事实与驱动期状态：节点经 ``_runtime_context()`` 读取。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.config.configuration import get_tool_registry
from app.config.logging.logger import log
from app.core.events import EventSink
from app.core.tool.background import restart_settlement
from app.core.workflow.cancel.conversation_run_cancellation_registry import (
    cancellation_registry,
)
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.models import ConversationRunRecord, TaskRecord, WorkspaceRecord
from app.core.workflow.message_queue.task_message import NoticeBody, TaskMessage
from app.service.depends import get_conversation_event_projector
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

if TYPE_CHECKING:
    from app.core.agents.agent_runtime_config import AgentRuntimeConfig
    from app.core.llm_provider.openai_model import OpenAIChatModel
    from app.task_runtime.task_runtime_space import TaskRuntimeSpace

# 准入通知的固定前缀：通知文本会成为 canonical 历史的一行，前缀同时充当稳定标识。
_NOTICE_PREFIX = "Tool availability notice:"


@dataclass
class RuntimeContext:
    """每次驱动重新装配，不参与恢复：数据事实在 Run 进度与 Task context。

    只承载随 Run / Task 变化的事实与对象；进程级单例（Run 状态写入、外部消息队列、工具系统）由节点经
    ``app.service.depends`` / ``app.config.configuration`` 直接取用，不在此转手。事件只经
    ``event_sink`` 回调交出，core 因此不依赖 Assistant Transport；节点用 ``node_helper.emit``
    发事件，sink 失败在那里降级。后一组字段由 ``ReactLikeWorkflow`` 在解析模型、读取进度后补齐。
    """

    # —— 装配方注入：本次 Run 的事实 ——
    run: ConversationRunRecord
    task: TaskRecord
    workspace: WorkspaceRecord
    # 本次 Run 的 Agent 运行时配置：Agent 定义与模型选择的最终解析，模型字段已完整。
    agent: AgentRuntimeConfig
    # 本轮准入的工具名；只在模型返回调用后拦截，模型绑定仍用 Task 固化的完整 schema。
    allows_tools: frozenset[str]
    task_space: TaskRuntimeSpace
    event_sink: EventSink
    # 本次执行的内部消息队列：调度方投递外部队列消息，节点投递 Run 内通知，prepare_input 取光。
    mailbox: RunMailbox

    # —— workflow 驱动期补齐 / 可变状态 ——
    model: OpenAIChatModel | None = None
    thinking_channel: str = ""
    usage_stats: ConversationRunUsageStats = field(default_factory=ConversationRunUsageStats)
    # 本次驱动起的标题支线（``branch.task_title``）；复用 ``model``，由 workflow 在关闭 client 前收尾。
    # 「本 Run 是否起过」看执行状态 ``title_requested``，不看这个字段。
    title_branch: asyncio.Task[None] | None = None

    @classmethod
    def for_run(cls, run: ConversationRunRecord) -> RuntimeContext:
        """为单个 Run 装配运行上下文，算出本轮的工具准入边界，并投递 Run 开始时的一次性通知。

        Task、工作区与 mailbox 取自 Run 所属 Task 的运行时空间（空间按 Task 只物化一次，这里不再
        重复读库）；Agent 配置由 ``AgentRuntimeConfig.for_run`` 合成，事件回调经
        ``app.service.depends`` 取 projector——调用方只需要 Run 这一条事实，而 core 仍不直接
        依赖 Assistant Transport。

        ``allows_tools`` 以 Task 固化的工具名为基准、减去本 Run 冻结的工具组按注册表展开的工具名
        （组名是用户意图的事实，展开在这里现算，前端不换算），只供工具执行层在
        模型返回调用后做准入拦截。模型绑定始终使用 Task 固化的完整 schema，不得按本轮准入过滤，
        否则会改变同一 Task 的请求前缀并破坏缓存复用。

        副作用:
            需要时向本次执行的 mailbox 投递本轮工具准入说明（与 space 上次告知的禁用集合比较，
            规则见 :meth:`tool_availability_notice`），由下一次 prepare_input 写入上下文，并更新
            space 的已告知集合。通知一旦消费即成为 canonical 历史的固定行，是后续 Run 请求前缀
            的一部分，不能改为「只注入本次请求、不落库」，否则下一个 Run 的前缀会从这里分叉。
            投递即视为已告知：通知并入前 Run 就被取消属低概率情形，被禁工具仍由准入拦截兜底。

            本进程首次驱动该 Task 时，按需再投一条「旧终端已失效」通知（见
            :meth:`_post_terminals_invalidated`）。两类通知同一口径：对照 space 上的已告知状态、
            只在需要时投递，Run 开始时要告诉 Agent 的事都在这里。
        """

        from app.core.agents.agent_runtime_config import AgentRuntimeConfig

        task_space = task_runtime_spaces.get_or_create(run.task_id)
        ban_tools = frozenset(
            get_tool_registry().tool_groups_to_tool_names(run.options.disabled_tool_groups)
        )
        allows_tools = frozenset(tool["name"] for tool in task_space.task_tool_schemas) - ban_tools
        content = cls.tool_availability_notice(
            task_space.announced_ban_tools, ban_tools, allows_tools
        )
        if content is not None:
            task_space.mailbox.post(
                TaskMessage.of(NoticeBody(content=content, notice_kind="tool_availability"))
            )
        task_space.announced_ban_tools = ban_tools
        cls._post_terminals_invalidated(task_space)
        return cls(
            run=run,
            task=task_space.current_task,
            workspace=task_space.current_workspace,
            agent=AgentRuntimeConfig.for_run(run),
            allows_tools=allows_tools,
            task_space=task_space,
            event_sink=get_conversation_event_projector().process,
            mailbox=task_space.mailbox,
        )

    @staticmethod
    def _post_terminals_invalidated(task_space: TaskRuntimeSpace) -> None:
        """本进程首次驱动该 Task 且它用过交互终端时，投递「旧终端已失效」通知；判定见
        ``restart_settlement.first_drive_notice``（同步读一次 context 表，每个 Task 每个进程至多一次）。

        失败只记录、不上抛：这里在 ``RunDriver`` 的异常边界内，上抛会让一条可有可无的提醒把 Run
        落定为 failed。
        """
        try:
            notice = restart_settlement.first_drive_notice(task_space)
        except Exception:
            log.exception(
                "terminals_invalidated_notice_failed",
                extra={"msg": "旧终端失效通知投递失败", "data": {"task_id": task_space.task_id}},
            )
            return
        if notice is not None:
            task_space.mailbox.post(notice)

    def is_cancelled(self) -> bool:
        """本 Task 是否收到停止信号；只读进程内注册表，节点在多个检查点调用，不查库。"""
        return cancellation_registry.is_cancelled(self.task.id)

    @staticmethod
    def tool_availability_notice(
        announced: frozenset[str] | None,
        banned: frozenset[str],
        allowed: frozenset[str],
    ) -> str | None:
        """返回本轮应投递的工具准入通知文本；``None`` 表示无需通知。

        ``announced`` 是上次已告知模型的禁用集合，``banned`` / ``allowed`` 是本 Run 冻结工具组展开后
        的禁用与可用工具名（``allows_tools``）。与上次比较、只在必要时生成：通知消费后是 canonical
        历史的固定行，每个 Run 都重复全量说明会让 context 持续膨胀。工具名一律排序，避免同一集合
        跨进程产生不同文本（前缀缓存）。

        - ``announced`` 为 ``None``（模型此前被告知了什么未知）：完整说明。
        - 集合有变化：说明新禁用 / 恢复的工具，并附当前完整禁用列表，使单条通知自洽。
        - 未变化但仍有禁用：简短提醒，防止模型在长对话里遗忘。
        - 未变化且全部可用：不通知。
        """

        if announced is None:
            return RuntimeContext._full_notice(banned, allowed)
        if banned == announced:
            if not banned:
                return None
            return (
                f"{_NOTICE_PREFIX} still disabled for this round, calls to them will be "
                f"rejected: {RuntimeContext._names(banned)}."
            )
        changes = []
        if newly_banned := banned - announced:
            changes.append(f"newly disabled: {RuntimeContext._names(newly_banned)}")
        if re_enabled := announced - banned:
            changes.append(f"re-enabled: {RuntimeContext._names(re_enabled)}")
        if not banned:
            current = "all tools are available now."
        elif not allowed:
            current = "all tools are disabled now."
        else:
            current = (
                f"currently disabled, calls to them will be rejected: "
                f"{RuntimeContext._names(banned)}."
            )
        return f"{_NOTICE_PREFIX} changed since last round ({'; '.join(changes)}); {current}"

    @staticmethod
    def _full_notice(banned: frozenset[str], allowed: frozenset[str]) -> str:
        """``announced`` 未知时的一次性完整说明：当前禁用清单与可用工具名。"""

        if not banned:
            return f"{_NOTICE_PREFIX} all tools are available for this round."
        if not allowed:
            return f"{_NOTICE_PREFIX} all tools are not available for this round."
        return (
            f"{_NOTICE_PREFIX} the following tools are disabled for this round and calls "
            f"to them will be rejected: {RuntimeContext._names(banned)}. "
            f"{RuntimeContext._names(allowed)} tools available."
        )

    @staticmethod
    def _names(tools: set[str] | frozenset[str]) -> str:
        """工具名排序后逗号分隔：同一集合跨进程生成同一文本，保住前缀缓存。"""

        return ", ".join(sorted(tools))
