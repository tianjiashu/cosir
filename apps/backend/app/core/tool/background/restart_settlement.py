"""后端重启收尾：让 Agent 知道重启前的后台工作已经结束。

登记表 ``deferred_registry`` 只在进程内、重启后不重建，重启前登记的条目再也不会有结论（设计见
``docs/plan/background-tool-result-design.md`` 规则 6）：

- **委派子 Agent**（:func:`settle_restarted_child_runs`）：子 Agent 不能挂起（没有
  ``wait_messages``、终端与审批工具），重启时它的 Run 一定是 running，已被恢复流程收敛为
  ``cancelled / runtime_restarted``。父 Agent 的等待随进程丢失，按子 Task 的 ``parent_task_id``
  直接回投这个结论（不对应某次调用，``tool_call_id`` 为 ``None``），唤醒等它的父 Agent。
- **终端**：会话与 ``execute_terminal`` 的后台命令同样随进程丢失，Agent 上下文里还留着旧
  ``session_id``，见下。
- **Agent Team** 目前前台执行：重启时调用它的父 Run 本身就是 running，已一起被收敛，不需要回投；
  第 5 步 Team 可后台执行后再接入。

终端的告知有两条路径，同一 Task 在一个进程里至多告知一次（``TaskRuntimeSpace.
terminals_invalidation_announced`` 记录，space 与进程同寿、只在删除时卸载）：

- **重启唤醒**（:func:`wake_suspended_terminal_runs`，由 ``RecoverService.recover_after_restart``
  调用）：当前 Run 挂起、且该 Run 调用过终端工具（交互终端或 ``execute_terminal``）的 Task，可能正等
  一条再也不会来的后台命令结论或交还；投递一条 ``backend_restart`` / ``backend_restarted`` 的后台结论
  把它唤醒（不对应某次调用，``tool_call_id`` 为 ``None``），正文说明后台命令已停止、全部终端已失效。
  交接（``terminal_handoff``）是 HITL：等待中的决定按契约保留不结算，之后交还会报告会话已不在。
- **首次驱动提醒**（:func:`first_drive_notice`，``RuntimeContext.for_run`` 每次装配时调用）：context 里
  调用过任一交互终端工具的 Task，在本进程第一次驱动时往本次执行的 mailbox 投一条 Run 内
  ``system_notice``（不落库，随 prepare_input 写入 context）。只认 ``terminal_start`` 不够：Agent
  可能只操作过用户新建的终端。

终端工具名按工具组从注册表展开，不在 core 里手列工具名（core 不依赖 ``app.tools``，工具面由工具层
自己声明分组）：首次驱动提醒只看交互终端组 ``TOOL_GROUP_TERMINAL_SESSION``（只有会话 id 会失效，
跑过 ``execute_terminal`` 的 Task 不需要提醒）；重启唤醒另加 ``TOOL_GROUP_TERMINAL``（后台命令也会
让 Run 挂起）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from app.config.logging.logger import log
from app.core.tool.background import placeholders
from app.core.tool.background.deferred_registry import deliver_background_result
from app.core.tool.schemas.tool_names import TOOL_DELEGATE_TASK
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL, TOOL_GROUP_TERMINAL_SESSION
from app.core.workflow.message_queue.task_message import NoticeBody, TaskMessage
from app.models import ConversationRunRecord
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.service import depends as service_depends

if TYPE_CHECKING:
    from app.task_runtime.task_runtime_space import TaskRuntimeSpace

_SETTLE_PARENT_STATUSES = frozenset(
    {ConversationRunStatus.SUSPENDED.value, ConversationRunStatus.COMPLETED.value}
)

INVALIDATED_NOTICE = (
    "The backend restarted. All interactive terminal sessions you opened before are gone and "
    "their session ids are no longer valid. Call terminal_start again if you still need a "
    "terminal."
)

RESTART_WAKE_NOTICE = (
    "The backend restarted. Background commands you started with execute_terminal were "
    "stopped, and all interactive terminal sessions are gone (their session ids are no longer "
    "valid). Run the commands again or call terminal_start if you still need them."
)


def _terminal_tool_names(*groups: str) -> frozenset[str]:
    from app.config.configuration import get_tool_registry

    return frozenset(get_tool_registry().tool_groups_to_tool_names(list(groups)))


def interrupt_pending_placeholders() -> int:
    """重启前仍为 ``pending`` 的后台占位全部推到 ``cancelled``（短提示「已中断」），返回行数。

    登记表重启后为空，这些调用不会再由撤销结算。委派占位同样停在「已中断」：重启结论不对应某次
    调用，并入时不改占位；委派卡片状态跟随子 Task 最新 Run，不受影响。
    """

    return placeholders.settle_placeholders(None, None, "cancelled", placeholders.INTERRUPTED_HINT)


def settle_restarted_child_runs(recovered: Iterable[ConversationRunRecord]) -> list[int]:
    """为恢复流程收敛的委派子 Run 回投 ``runtime_restarted`` 结论，返回已回投的子 Run id。

    须在 ``bind_loop`` 之后调用（入队会拉起父 Task 的 actor），编排见 ``RecoverService``。
    只回投给当前 Run 挂起或已完成的父 Task：挂起的在等它，已完成的照常因后台结论开新 Run；
    父 Run 自己也在重启时被收敛为 cancelled 的不回投——那是被打断的执行，由用户决定是否继续，
    不由后台结论隐式推起来。单个失败记录后继续。
    """

    candidates = list(recovered)
    if not candidates:
        return []
    from app.core.tool.handler.child_agent.child_run_job import child_run_result

    tasks = service_depends.get_task_service()
    settled: list[int] = []
    for run in candidates:
        try:
            task = tasks.get_task(run.task_id)
            if task.task_type != TOOL_DELEGATE_TASK or task.parent_task_id is None:
                continue
            parent_run = tasks.require_current_run(task.parent_task_id)
            if parent_run is None or parent_run.status not in _SETTLE_PARENT_STATUSES:
                continue
            if deliver_background_result(
                task.parent_task_id,
                tool_call_id=None,
                tool_name="child_agent",
                result=child_run_result(run.task_id, run),
            ):
                settled.append(run.id)
        except Exception:
            log.exception(
                "child_restart_settlement_failed",
                extra={"msg": "重启后回投子 Agent 结论失败", "data": {"run_id": run.id}},
            )
    if settled:
        log.info(
            "child_runs_settled_after_restart",
            extra={"msg": "重启后已向父 Agent 回投子 Agent 结论", "data": {"run_ids": settled}},
        )
    return settled


def wake_suspended_terminal_runs(task_ids: Iterable[int]) -> list[int]:
    """为 ``task_ids`` 中挂起在终端上的 Task 投递重启结论，返回已投递的 task_id。

    启动期调用、同步读库；候选集合是启动时需要调度的 Task，由重启恢复入口传入，与拉起 actor 用
    同一份，不重复查询。

    「挂起在终端上」按该 Run 是否调用过终端工具（交互终端或 ``execute_terminal``）判定：Run 只记录
    挂起本身，不记录在等什么。误判（同一 Run 里用过终端、实际在等子 Agent）的代价只是多一次唤醒，
    模型可以再次 ``wait_messages``。
    单个 Task 失败记录后继续，不阻断启动。
    """

    from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

    candidates = sorted(task_ids)
    if not candidates:
        return []
    tasks = service_depends.get_task_service()
    contexts = service_depends.get_conversation_task_context_service()
    names: frozenset[str] | None = None
    woken: list[int] = []
    for task_id in candidates:
        try:
            current = tasks.require_current_run(task_id)
            if current is None or current.status != ConversationRunStatus.SUSPENDED.value:
                continue
            names = names if names is not None else _terminal_tool_names(
                TOOL_GROUP_TERMINAL_SESSION, TOOL_GROUP_TERMINAL
            )
            if not contexts.has_tool_call(task_id, names, run_id=current.id):
                continue
            # 先记已告知再投递：投递会让 actor 唤醒 Run，首次驱动提醒据此不再重复。
            task_runtime_spaces.get_or_create(task_id).terminals_invalidation_announced = True
            delivered = deliver_background_result(
                task_id,
                tool_call_id=None,
                tool_name="backend_restart",
                result={
                    "end_reason": "backend_restarted",
                    "message": RESTART_WAKE_NOTICE,
                },
            )
            if delivered:
                woken.append(task_id)
        except Exception:
            log.exception(
                "terminal_restart_wake_failed",
                extra={"msg": "重启后唤醒终端等待失败", "data": {"task_id": task_id}},
            )
    if woken:
        log.info(
            "terminal_runs_woken_after_restart",
            extra={"msg": "重启后已唤醒挂起在终端上的 Run", "data": {"task_ids": woken}},
        )
    return woken


def first_drive_notice(space: TaskRuntimeSpace) -> TaskMessage | None:
    """本进程首次驱动该 Task 时，若 context 调用过交互终端工具，返回一条失效通知。

    无论是否需要告知都置位：首次驱动之后本进程开出的终端都是活的，不再需要提醒。同步读库
    （每个 Task 每个进程至多一次）。同一 Task 的 Run 由 actor 串行发起，无需加锁。
    """

    if space.terminals_invalidation_announced:
        return None
    space.terminals_invalidation_announced = True
    contexts = service_depends.get_conversation_task_context_service()
    if not contexts.has_tool_call(space.task_id, _terminal_tool_names(TOOL_GROUP_TERMINAL_SESSION)):
        return None
    log.info(
        "terminals_invalidated_notice_posted",
        extra={"msg": "首次驱动，提醒 Agent 旧终端已失效", "data": {"task_id": space.task_id}},
    )
    return TaskMessage.of(
        NoticeBody(content=INVALIDATED_NOTICE, notice_kind="terminals_invalidated")
    )
