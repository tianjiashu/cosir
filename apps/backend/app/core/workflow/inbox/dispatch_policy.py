"""Task 消息调度规则：纯函数，不读写数据库、不触碰执行器，可逐条单测。

Task 的调度 actor（``TaskActor``）读出最新 Run 与队列，交给这里判定，再执行判定结果。规则只读
消息属性（``kind`` / ``delivery``）与 Run 状态，投递方不另加判断。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import ConversationRunRecord
from app.models.enums.conversation_run_status import ACTIVE_RUN_STATUSES, ConversationRunStatus
from app.core.workflow.inbox.task_message import (
    RunLaunch,
    TaskMessage,
    UserInputBody,
)


@dataclass(frozen=True)
class Deliver:
    """把这些消息投进当前执行的 mailbox。"""

    messages: list[TaskMessage]


@dataclass(frozen=True)
class Wake:
    """挂起的 Run 继续：``suspended → running``，同一个 Run 从 prepare_input 进入。"""


@dataclass(frozen=True)
class StartRun:
    """以该配置新建 Run 并启动执行。"""

    launch: RunLaunch


@dataclass(frozen=True)
class Stay:
    """不动。"""


Action = Deliver | Wake | StartRun | Stay


def next_action(
    latest: ConversationRunRecord | None,
    has_executor: bool,
    pending: list[TaskMessage],
    *,
    by_user: bool,
) -> Action:
    """推进规则的唯一实现（设计见 ``docs/plan/task-mailbox-suspend-wake-design.md`` 第 7 节）。

    - 有执行者：投递全部 ``immediate`` 消息，``next_run`` 留给下一个 Run。
    - Agent 挂起（``suspended``）：有可唤醒的 ``immediate`` 消息就唤醒同一个 Run，否则不动。
    - 已完成：有用户输入时交接新 Run；只有结果消息时也新建 Run 继续处理，配置继承最新 Run。
    - 用户挂起（取消）、失败或尚无 Run：只有用户发起的推进（``by_user``）才从用户输入交接新 Run，
      其余消息只留在队列里，随下一次用户输入一并并入。

    交接新 Run 的配置取**最后**一条用户输入：剩余消息全部并入这个 Run，最后一条是用户最新的选择
    （取消后残留的旧输入、先排队的 ``next_run`` 输入都让位于最新一次发送）。
    """
    if has_executor:
        return Deliver([message for message in pending if message.delivery == "immediate"])
    status = latest.status if latest is not None else None
    if status == ConversationRunStatus.SUSPENDED:
        wakes = any(
            message.delivery == "immediate" and type(message.body).WAKES_SUSPENDED
            for message in pending
        )
        return Wake() if wakes else Stay()
    if status in ACTIVE_RUN_STATUSES:
        return Stay()
    launches = [
        message.body.launch for message in pending if isinstance(message.body, UserInputBody)
    ]
    completed = status == ConversationRunStatus.COMPLETED
    if launches and (by_user or completed):
        return StartRun(launches[-1])
    if completed and any(type(message.body).WAKES_COMPLETED for message in pending):
        inherited = _launch_of(latest)
        return StartRun(inherited) if inherited is not None else Stay()
    return Stay()

def _launch_of(run: ConversationRunRecord | None) -> RunLaunch | None:
    """沿用一个 Run 的发起配置；模型配置已被删除时无法继承。"""
    if run is None or run.agent_id is None or run.model_config_id is None:
        return None
    return RunLaunch(
        agent_id=run.agent_id, model_config_id=run.model_config_id, options=run.options
    )


def matches_run(launch: RunLaunch, run: ConversationRunRecord) -> bool:
    """发起配置是否与该 Run 一致；生产者据此为运行中发送选择 ``delivery``。"""
    return (
        launch.agent_id == run.agent_id
        and launch.model_config_id == run.model_config_id
        and launch.options == run.options
    )
