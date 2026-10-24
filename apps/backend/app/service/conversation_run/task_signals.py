"""Task actor 的信号与入口回执。

信号只表达「醒来看看」的原因与随行参数，消息正文的事实源始终是外部队列（``task_messages``）：
信号丢失或重复都不影响正确性，actor 每次处理都重读事实源。``receipt`` 是入口等待的 future，
actor 处理完该信号后兑现；actor 之间互发的信号不带回执，不等对方处理。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.core.workflow.cancel.conversation_run_cancellation_registry import CancelReason
from app.models.task_message import TaskMessage, UserInputBody

if TYPE_CHECKING:
    from app.service.conversation_run.task_actor import Execution

Receipt = asyncio.Future[Any] | None


class SessionConflictError(ValueError):
    """当前 Task 状态不允许本次操作；接入层映射为 409，``code`` 是稳定的机器码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SessionReceipt:
    """``queued`` 为真表示本次没有新建、唤醒 Run，消息进了外部队列（运行中已投递给当前执行）。

    不回传 run_id：调用方（含子 Agent 工具与 Team 节点）只按 Task 寻址，需要等结论时用
    ``WorkflowSession.wait_idle``，再读 Task 的最新 Run。
    """

    queued: bool


@dataclass(frozen=True)
class Enqueue:
    """一条消息入外部队列后推进。``admit`` 为真时先按 ``dispatch_policy.admit`` 过滤。"""

    message: TaskMessage
    by_user: bool
    admit: bool
    receipt: Receipt = None


@dataclass(frozen=True)
class DeliverPending:
    """配置广播已落库：只投递给正在执行的 Run，不推进。"""

    receipt: Receipt = None


@dataclass(frozen=True)
class Cancel:
    reason: CancelReason
    receipt: Receipt = None


@dataclass(frozen=True)
class Stop:
    """Task 即将删除：停止当前执行与挂起的 Run，停下后 actor 退出。"""

    receipt: Receipt = None


@dataclass(frozen=True)
class Resume:
    receipt: Receipt = None


@dataclass(frozen=True)
class Rerun:
    """编辑已并入的最新用户输入：停止当前 Run 后移出旧内容，以新消息重跑。"""

    message_id: int
    body: UserInputBody
    receipt: Receipt = None


@dataclass(frozen=True)
class RunFinished:
    """执行自身在 ``drive_run`` 返回后发出；进程关闭取消执行时不发。"""

    execution: Execution
    receipt: Receipt = None


Signal = Enqueue | DeliverPending | Cancel | Stop | Resume | Rerun | RunFinished


def resolve(receipt: Receipt, value: Any) -> None:
    if receipt is not None and not receipt.done():
        receipt.set_result(value)


def reject(signal: Signal, exc: BaseException) -> None:
    if signal.receipt is not None and not signal.receipt.done():
        signal.receipt.set_exception(exc)


def abandon(signal: Signal) -> None:
    if signal.receipt is not None and not signal.receipt.done():
        signal.receipt.cancel()
