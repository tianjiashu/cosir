"""驱动开始时推导的通知：Run 级（工具准入）与回合级（用户已停止）。

这两类通知说的是本 Run 或上一回合的事实，不是只在某一刻存在的事件，所以不入队：每次驱动开始、
上下文重建之后（``begin_run``）从持久化事实与上下文现推，投进本次驱动的 mailbox，由 prepare_input
写入。比对基准都取自上下文里模型实际看到的通知，不取进程内记忆，因此重启、缓存卸载、撤回与分叉之后
推导仍然成立；撤回移出了某条通知，下一次驱动就重新推出它。通知分级见 ``NOTICE_LEVELS``。

推出的通知一旦写入就是 canonical 历史的固定行、后续请求前缀的一部分，不能改为「只注入本次请求、
不落库」。投进 mailbox 后还没写入 Run 就被取消属低概率情形：下一次驱动会照常再推一次。
"""

from __future__ import annotations

from typing import cast

from app.config.constant import Constant
from app.core.context.context_store import ContextStore
from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager
from app.core.workflow.execution_state.runtime_context import RuntimeContext
from app.core.workflow.message_queue.message_bodies.notice import NoticeBody

# 准入通知的固定前缀：通知文本会成为 canonical 历史的一行，前缀同时充当稳定标识。
_TOOL_NOTICE_PREFIX = "Tool availability notice:"
_USER_STOPPED = (
    "The user stopped your previous turn before it finished. Background work you started keeps "
    "running and its results will still arrive."
)


def drive_notices(rc: RuntimeContext, context: RuntimeMessageContextManager) -> list[NoticeBody]:
    """本次驱动开始时应投递的通知；调用方在 ``begin_run`` 之后调用（要读重建后的上下文）。"""
    notices = [_user_stopped(rc), _tool_availability(rc, context)]
    return [notice for notice in notices if notice is not None]


def _user_stopped(rc: RuntimeContext) -> NoticeBody | None:
    """上一次驱动以用户停止结束、且被停下的回合模型仍看得到时，告诉它这一回合没做完。

    事实源是 ``preceding_run`` 的结束原因（取消时按信号来源落库）：新 Run 看上一个 Run，恢复看
    本 Run 迁回 running 前的记录。上游停止（``parent_cancelled``）不留通知：Agent 的上游会自己交代。
    被停下的回合已被撤回时不说：模型看不到它，提起只会让它困惑。
    """
    preceding = rc.preceding_run
    if (
        preceding is None
        or preceding.end_reason != Constant.Run.RUN_END_REASON_USER_CANCELLED
        or ContextStore().run_last_turn_withdrawn(rc.task.id, preceding.id)
    ):
        return None
    return NoticeBody(content=_USER_STOPPED, notice_kind="user_cancelled")


def _tool_availability(
    rc: RuntimeContext, context: RuntimeMessageContextManager
) -> NoticeBody | None:
    """本 Run 的工具准入与模型上次被告知的不同时说明；本 Run 已说过且没变时不重复。

    基准是上下文里最近一条准入通知记下的禁用集合。它出自本 Run（挂起后唤醒、恢复）且集合没变就不说；
    出自更早的 Run 或继承来的历史时，按 :func:`tool_availability_notice` 的规则说明（新 Run 的第一次
    驱动因此仍会提醒一次仍在禁用的工具）。
    """
    task_banned, run_banned = rc.task_banned, rc.run_banned
    banned = task_banned | run_banned
    latest = context.latest_notice("tool_availability")
    announced = (
        None if latest is None else frozenset(cast(list[str], latest[1].banned_tools))
    )
    if latest is not None and latest[0] == rc.run.id and announced == banned:
        return None
    content = tool_availability_notice(
        announced, task_banned, run_banned, all_banned=not rc.admitted_tools
    )
    if content is None:
        return None
    return NoticeBody(
        content=content, notice_kind="tool_availability", banned_tools=sorted(banned)
    )


def tool_availability_notice(
    announced: frozenset[str] | None,
    task_banned: frozenset[str],
    run_banned: frozenset[str],
    *,
    all_banned: bool,
) -> str | None:
    """返回应投递的工具准入通知文本；``None`` 表示无需通知。

    ``announced`` 是上次已告知模型的禁用集合，``task_banned`` / ``run_banned`` 是本 Run 两个来源的
    禁用工具名（Task 级：Task 类型规则与派生深度；Run 级：本 Run 关掉的工具组），两者都只含该
    Task 手里有的工具。``all_banned`` 表示这些禁用覆盖了该 Task 的全部工具。

    比较用两个来源的并集——集合相同意味着可调用面相同，模型不需要知道原因换过；文本则按来源分组，
    让模型读到「这个 Task 不能派生」，而不是笼统的「这次禁用、下次也许可用」。工具名一律排序，避免
    同一集合跨进程产生不同文本（前缀缓存）。

    只报禁用、不枚举可用：可用集合由 Task 固化的 schema 与系统提示词的工具目录表达，在通知里再列
    一遍既拉长这段 canonical 历史，又会紧跟在禁用清单后面被读成「这些都被禁用」。

    - ``announced`` 为 ``None``（模型从没被告知过）：完整说明。
    - 集合有变化：说明新禁用 / 恢复的工具，并附当前禁用清单（按来源），使单条通知自洽。
    - 未变化但仍有禁用：简短提醒，防止模型在长对话里遗忘。
    - 未变化且全部可用：不通知。
    """

    banned = task_banned | run_banned
    if announced is None:
        return _full_notice(task_banned, run_banned, all_banned)
    if banned == announced:
        if not banned:
            return None
        groups = _disabled_groups(task_banned, run_banned)
        return f"{_TOOL_NOTICE_PREFIX} still disabled ({groups})."
    changes = []
    if newly_banned := banned - announced:
        changes.append(f"newly disabled: {_names(newly_banned)}")
    if re_enabled := announced - banned:
        changes.append(f"re-enabled: {_names(re_enabled)}")
    if all_banned:
        current = "all tools are disabled now"
    elif not banned:
        current = "all tools are available now"
    else:
        current = f"currently disabled ({_disabled_groups(task_banned, run_banned)})"
    return f"{_TOOL_NOTICE_PREFIX} changed since last round ({'; '.join(changes)}); {current}."


def _full_notice(task_banned: frozenset[str], run_banned: frozenset[str], all_banned: bool) -> str:
    """模型从没被告知过时的完整说明：按来源给当前禁用清单；全部被禁时只给一句结论。"""

    if all_banned:
        return f"{_TOOL_NOTICE_PREFIX} all tools are disabled."
    if not task_banned and not run_banned:
        return f"{_TOOL_NOTICE_PREFIX} all tools are available."
    return f"{_TOOL_NOTICE_PREFIX} disabled ({_disabled_groups(task_banned, run_banned)})."


def _disabled_groups(task_banned: frozenset[str], run_banned: frozenset[str]) -> str:
    """按来源给出禁用清单：``for this task: …; for this run: …``。"""

    groups = []
    if task_banned:
        groups.append(f"for this task: {_names(task_banned)}")
    if run_banned:
        groups.append(f"for this run: {_names(run_banned)}")
    return "; ".join(groups)


def _names(tools: frozenset[str]) -> str:
    """工具名排序后逗号分隔：同一集合跨进程生成同一文本，保住前缀缓存。"""

    return ", ".join(sorted(tools))
