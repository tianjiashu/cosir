"""一次 Run 驱动的 Run / Task 级事实与驱动期状态：节点经 ``_runtime_context()`` 读取。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.config.configuration import get_tool_registry
from app.core.events import EventSink
from app.core.tool.multi_agent.task_tool_scope import banned_derivation_tools
from app.core.workflow.cancel.conversation_run_cancellation_registry import (
    cancellation_registry,
)
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.core.workflow.message_queue.task_message import NoticeBody
from app.models import ConversationRunRecord, TaskRecord, WorkspaceRecord
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
    admitted_tools: frozenset[str]
    task_space: TaskRuntimeSpace
    event_sink: EventSink
    # 进入本次执行的唯一通道：调度方投来的外部队列行副本，与执行内通知（工具准入、旧终端失效、
    # 续写，经 ``mailbox.notify``，不落库）；prepare_input 取光。
    mailbox: RunMailbox

    # —— workflow 驱动期补齐 / 可变状态 ——
    model: OpenAIChatModel | None = None
    thinking_channel: str = ""
    usage_stats: ConversationRunUsageStats = field(default_factory=ConversationRunUsageStats)
    # 本次驱动起的标题支线（``branch.task_title``）；复用 ``model``，由 workflow 在关闭 client 前
    # 收尾。「本 Run 是否起过」看执行状态 ``title_requested``，不看这个字段。
    title_branch: asyncio.Task[None] | None = None

    @classmethod
    def for_run(cls, run: ConversationRunRecord) -> RuntimeContext:
        """为单个 Run 装配运行上下文，算出本轮的工具准入边界，并备好 Run 开始时的一次性通知。

        Task、工作区与 mailbox 取自 Run 所属 Task 的运行时空间（空间按 Task 只物化一次，这里不再
        重复读库）；Agent 配置由 ``AgentRuntimeConfig.for_run`` 合成，事件回调经
        ``app.service.depends`` 取 projector——调用方只需要 Run 这一条事实，而 core 仍不直接
        依赖 Assistant Transport。

        ``admitted_tools`` 以 Task 固化的工具名为基准，减去两个来源：Task 级（这类 Task 不允许的
        派生方式、派生深度已到上限）与 Run 级（本 Run 冻结的工具组按注册表展开，组名是用户意图的
        事实，展开在这里现算，前端不换算）。两个来源分开保留，通知据此说明禁用原因；它们都按本 Run
        现算，所以改 ``AGENT_MAX_DERIVATION_DEPTH`` 对下一次 Run 即时生效——但只对 schema 里仍有
        派生工具的 Task（fork）有效，schema 已固化的子 Task 在创建时就没有那些工具声明（见
        ``task_tool_scope`` 模块说明）。准入只供工具执行层在模型返回调用后拦截。模型绑定始终使用
        Task 固化的完整 schema，不得按本轮准入过滤，否则会改变同一 Task 的请求前缀并破坏缓存复用。

        副作用:
            需要时把本轮工具准入说明投进 mailbox（与 space 上次告知的禁用集合比较，
            规则见 :meth:`tool_availability_notice`），由下一次 prepare_input 写入上下文，并更新
            space 的已告知集合。通知一旦消费即成为 canonical 历史的固定行，是后续 Run 请求前缀
            的一部分，不能改为「只注入本次请求、不落库」，否则下一个 Run 的前缀会从这里分叉。
            备好即视为已告知：通知并入前 Run 就被取消属低概率情形，被禁工具仍由准入拦截兜底。

            重启中断的通知不走这里：它由 ``RecoverService`` 在启动时投进外部队列，随下一次驱动
            并入（见 ``RecoverService.notify_interrupted``）。
        """

        from app.core.agents.agent_runtime_config import AgentRuntimeConfig

        task_space = task_runtime_spaces.get_or_create(run.task_id)
        task = task_space.current_task
        task_tools = frozenset(task_space.task_tool_names)
        # 两个禁用来源分开算，不在这里并成一个集合：通知要按来源说清「为什么不能用」，来源信息一旦
        # 被并集吞掉，下游只能笼统表述成「本轮禁用」。两者都先与 ``task_tools`` 求交——模型手里没有
        # 的工具不算「被禁」，通知不该提它。
        task_banned = banned_derivation_tools(task.task_type, task.derivation_depth) & task_tools
        run_banned = (
            frozenset(
                get_tool_registry().tool_groups_to_tool_names(run.options.disabled_tool_groups)
            )
            & task_tools
        )
        admitted_tools = task_tools - task_banned - run_banned
        # 执行开始时 mailbox 已重置（``TaskExecution.start``），这里投进的通知随本次执行写入或丢弃。
        content = cls.tool_availability_notice(
            task_space.announced_ban_tools,
            task_banned,
            run_banned,
            all_banned=not admitted_tools,
        )
        if content is not None:
            task_space.mailbox.notify(NoticeBody(content=content, notice_kind="tool_availability"))
        task_space.announced_ban_tools = task_banned | run_banned

        return cls(
            run=run,
            task=task,
            workspace=task_space.current_workspace,
            agent=AgentRuntimeConfig.for_run(run),
            admitted_tools=admitted_tools,
            task_space=task_space,
            event_sink=get_conversation_event_projector().process,
            mailbox=task_space.mailbox,
        )

    def is_cancelled(self) -> bool:
        """本 Task 是否收到停止信号；只读进程内注册表，节点在多个检查点调用，不查库。"""
        return cancellation_registry.is_cancelled(self.task.id)

    @staticmethod
    def tool_availability_notice(
        announced: frozenset[str] | None,
        task_banned: frozenset[str],
        run_banned: frozenset[str],
        *,
        all_banned: bool,
    ) -> str | None:
        """返回本轮应投递的工具准入通知文本；``None`` 表示无需通知。

        ``announced`` 是上次已告知模型的禁用集合，``task_banned`` / ``run_banned`` 是本轮两个来源的
        禁用工具名（Task 级：Task 类型规则与派生深度；Run 级：本 Run 关掉的工具组），两者都只含该
        Task 手里有的工具。``all_banned`` 表示这些禁用覆盖了该 Task 的全部工具。

        与上次比较、只在必要时生成：通知消费后是 canonical 历史的固定行，每个 Run 都重复全量说明会让
        context 持续膨胀。比较用两个来源的并集——集合相同意味着可调用面相同，模型不需要知道原因换过；
        文本则按来源分组，让模型读到「这个 Task 不能派生」，而不是笼统的「本轮禁用、下轮也许可用」。
        工具名一律排序，避免同一集合跨进程产生不同文本（前缀缓存）。

        只报禁用、不枚举可用：可用集合由 Task 固化的 schema 与系统提示词的工具目录表达，在通知里再列
        一遍既拉长这段 canonical 历史，又会紧跟在禁用清单后面被读成「这些都被禁用」。

        - ``announced`` 为 ``None``（模型此前被告知了什么未知）：完整说明。
        - 集合有变化：说明新禁用 / 恢复的工具，并附当前禁用清单（按来源），使单条通知自洽。
        - 未变化但仍有禁用：简短提醒，防止模型在长对话里遗忘。
        - 未变化且全部可用：不通知。
        """

        banned = task_banned | run_banned
        if announced is None:
            return RuntimeContext._full_notice(task_banned, run_banned, all_banned)
        if banned == announced:
            if not banned:
                return None
            return (
                f"{_NOTICE_PREFIX} still disabled "
                f"({RuntimeContext._disabled_groups(task_banned, run_banned)})."
            )
        changes = []
        if newly_banned := banned - announced:
            changes.append(f"newly disabled: {RuntimeContext._names(newly_banned)}")
        if re_enabled := announced - banned:
            changes.append(f"re-enabled: {RuntimeContext._names(re_enabled)}")
        if all_banned:
            current = "all tools are disabled now"
        elif not banned:
            current = "all tools are available now"
        else:
            current = (
                f"currently disabled ({RuntimeContext._disabled_groups(task_banned, run_banned)})"
            )
        return f"{_NOTICE_PREFIX} changed since last round ({'; '.join(changes)}); {current}."

    @staticmethod
    def _full_notice(
        task_banned: frozenset[str], run_banned: frozenset[str], all_banned: bool
    ) -> str:
        """``announced`` 未知时的一次性完整说明：按来源给当前禁用清单。

        可用集合不在这里枚举：它由 Task 固化的 schema 与系统提示词的工具目录表达，再列一遍既拉长
        这段 canonical 历史，也会让随后那条清单被读成禁用清单的一部分。全部工具都被禁时只给一句
        结论，逐个列名反而更长。
        """

        if all_banned:
            return f"{_NOTICE_PREFIX} all tools are disabled."
        if not task_banned and not run_banned:
            return f"{_NOTICE_PREFIX} all tools are available."
        return (
            f"{_NOTICE_PREFIX} disabled "
            f"({RuntimeContext._disabled_groups(task_banned, run_banned)})."
        )

    @staticmethod
    def _disabled_groups(task_banned: frozenset[str], run_banned: frozenset[str]) -> str:
        """按来源给出禁用清单：``for this task: …; for this run: …``。"""

        groups = []
        if task_banned:
            groups.append(f"for this task: {RuntimeContext._names(task_banned)}")
        if run_banned:
            groups.append(f"for this run: {RuntimeContext._names(run_banned)}")
        return "; ".join(groups)

    @staticmethod
    def _names(tools: set[str] | frozenset[str]) -> str:
        """工具名排序后逗号分隔：同一集合跨进程生成同一文本，保住前缀缓存。"""

        return ", ".join(sorted(tools))
