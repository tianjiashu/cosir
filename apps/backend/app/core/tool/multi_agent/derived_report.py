"""派生汇报：派生 Task 的 Run 落定、已批准的 Agent Team 落定时，把结论交给发起方——多 Agent 编排的回程。

委派与 Agent fork 是编排关系，不是「还没跑完的工具调用」：派生工具当场完成，之后的进展属于派生
Task 自己。它的每个 Run 落定（完成、失败、被用户停止）都汇报一次，按 Task 关联、不按调用关联：
父 Agent 连发几条消息并进同一个 Run 时只汇报一次；之后被唤醒（后台命令结束、孙 Task 汇报、父 Agent
追加消息）再落定时再汇报。挂起不算落定：子 Agent 用 ``wait_messages`` 等下游时不会提前汇报。

汇报对象只看派生关系（``TaskRecord.deriver_id()``），不看「是谁开启了这个 Run」：汇报本身是一条
Agent 消息，会开启发起方的 Run，若按发送方回报，父子会互相回报停不下来；派生关系只朝上走，天然
收敛。Team 节点与用户手动 fork 没有派生方，不汇报——节点结论由 Coordinator 读。

不汇报的情形：
- Run 由用户输入开启（用户在工作台或侧栏直接和它对话）：那是用户与它之间的事；
- 上游收回（``parent_cancelled``）：发起方已不要它的结果；
- 删除、重启、进程关闭：这些路径不经 actor 的落定点。
被用户停止或删除时不交结论，只给发起方留一条 Task 级通知（:func:`report_deleted` 由删除入口调用）。
通知只唤醒发起方挂起的 Run、不开新 Run（``NOTICE_WAKE_SCOPES``，与单停后台命令的回执一致）：
发起方若正在 ``wait_messages`` 上等它，没有这条通知就一直挂着；发起方空闲时，用户刚停掉的工作
不该让 Agent 自己动起来。

Agent Team 同属编排：批准后 Coordinator 运行节点，TeamRun 落定时由它在唯一收尾处调用
:func:`report_team_settled`，汇报给主 Task（来源是那次 TeamRun）。被上游收回、撤回、删除或关闭停下的
不汇报，被用户单独停止的只留一条停止通知，规则与派生 Task 一致。

调用方是 ``TaskActor`` 的 Run 落定点与 Coordinator 的收尾处，在工作线程里调用；它们只决定时机，规则
都在这里。
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable
from typing import cast

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.context.context_store import ContextStore
from app.core.tool.background_commands import background_commands
from app.core.workflow.message_queue.task_message import (
    AgentMessageBody,
    NoticeBody,
    RunLaunch,
    TaskMessage,
)
from app.models import ConversationRunRecord, TaskRecord
from app.models.conversation_input import AgentReport, MessageSource
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.models.enums.conversation_run_status import ACTIVE_RUN_STATUSES, ConversationRunStatus
from app.models.message_metadata import UserMessageMetadata
from app.service import depends as service_depends
from app.storage.crud.agent_team_run_crud import AgentTeamRunCrud
from app.storage.model.agent_team_run_model import AgentTeamRunModel


def report_settled(run: ConversationRunRecord) -> None:
    """``run`` 已落定为终态：按规则向派生方投递汇报或停止通知；不该汇报时什么也不做。

    派生方已被删除（fork 不随来源删除）时投递失败，记录后放弃。其余异常上浮给调用方记录：
    汇报失败不改变子 Task 自己的结局。
    """
    tasks = service_depends.get_task_service()
    task = tasks.get_task(run.task_id)
    deriver_id = task.deriver_id()
    if (
        deriver_id is None
        or run.end_reason == Constant.Run.RUN_END_REASON_PARENT_CANCELLED
        or _opened_by_user(run)
    ):
        return
    if run.status == ConversationRunStatus.CANCELLED.value:
        _deliver(
            deriver_id,
            _stopped_notice(
                f"task {task.id} ({task.title}), which you derived,", "this run"
            ),
            data={"task_id": task.id, "run_id": run.id},
        )
        return
    report = AgentReport(
        status="completed" if run.status == ConversationRunStatus.COMPLETED.value else "failed",
        intermediate=has_open_work(task.id),
    )
    _deliver_report(
        deriver_id,
        _report_text(run),
        MessageSource(task_id=task.id, name=task.title, report=report),
        data={"task_id": task.id, "run_id": run.id, "status": run.status},
    )


def report_team_settled(row: AgentTeamRunModel, payload: dict[str, object], *, stopped_by_user: bool) -> None:
    """已批准的 Team 落定（``row`` 是终态 TeamRun）：向主 Task 汇报结论，被用户单独停止时只留通知。

    ``payload`` 是给主 Agent 的结论正文（状态、结束原因与档案读法，见 ``team_result_payload``）。调用方
    已排除不该汇报的停止（上游收回、撤回、删除、关闭）。
    """
    name = str(row.configuration_snapshot_json["name"])
    data: dict[str, object] = {"team_run_id": row.id, "status": row.status}
    if stopped_by_user:
        _deliver(
            row.parent_task_id,
            _stopped_notice(
                f"the Agent Team {name} (team run {row.id})",
                "it; review the completed nodes in its archive",
                detail=json.dumps(payload, ensure_ascii=False),
            ),
            data=data,
        )
        return
    report = AgentReport(
        status="completed" if row.status == AgentTeamRunStatus.COMPLETED.value else "failed",
        intermediate=False,
    )
    _deliver_report(
        row.parent_task_id,
        json.dumps(payload, ensure_ascii=False),
        MessageSource(team_run_id=row.id, name=name, report=report),
        data=data,
    )


def report_deleted(active_derived: Iterable[TaskRecord], deleted: Collection[int]) -> None:
    """用户删除了仍在活动的派生 Task：给仍在的派生方各留一条停止通知；派生方也被删了就不发。

    ``active_derived`` 由删除入口在停止之前读出（停下之后就都不活动了）：不活动的派生 Task 已经
    汇报过最后一次，派生方没有在等它。
    """
    for task in active_derived:
        deriver_id = task.deriver_id()
        if deriver_id is None or deriver_id in deleted:
            continue
        _deliver(
            deriver_id,
            _stopped_notice(f"task {task.id} ({task.title}), which you derived,", "it", verb="deleted"),
            data={"task_id": task.id},
        )


def _stopped_notice(
    subject: str, unreported: str, *, detail: str | None = None, verb: str = "stopped"
) -> TaskMessage:
    content = (
        f"The user {verb} {subject} before it finished. It will not report {unreported}; "
        "do not restart that work unless the user asks."
    )
    return TaskMessage.of(
        NoticeBody(
            content=content if detail is None else f"{content}\n{detail}",
            notice_kind="derived_task_stopped",
        )
    )


def _deliver_report(
    deriver_id: int, text: str, source: MessageSource, *, data: dict[str, object]
) -> None:
    """以发起方当前 Run 的配置投递一条汇报：汇报开出的新 Run 是发起方自己的，配置沿用它的。"""
    launch = RunLaunch.from_run(service_depends.get_task_service().require_current_run(deriver_id))
    if launch is None:
        # 发起方总是先有 Run 才能派生；没有可沿用的配置说明它的记录残缺，交不出可开 Run 的消息。
        log.warning(
            "derived_report_dropped_no_launch",
            extra={
                "msg": "发起方没有可沿用的发起配置，汇报放弃",
                "data": {"deriver_id": deriver_id, **data},
            },
        )
        return
    _deliver(deriver_id, TaskMessage.of(AgentMessageBody(text=text, source=source, launch=launch)), data=data)


def _deliver(deriver_id: int, message: TaskMessage, *, data: dict[str, object]) -> None:
    from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

    try:
        task_runtime_spaces.enqueue(deriver_id, message)
    except KeyError:
        log.info(
            "derived_report_dropped_deriver_missing",
            extra={"msg": "发起方已删除，汇报放弃", "data": {"deriver_id": deriver_id, **data}},
        )
        return
    log.info(
        "derived_report_delivered",
        extra={
            "msg": "已向发起方汇报",
            "data": {"deriver_id": deriver_id, "kind": message.kind, **data},
        },
    )


def has_open_work(task_id: int) -> bool:
    """Task 的下游还有没了结的工作：运行中的后台命令、运行中的 TeamRun，或活动的派生 Task。

    汇报据此标「中间结果」，Coordinator 等节点据此判断节点是否真的做完（``TaskActor._quiescent``）。
    派生 Task 是否活动只看它最新 Run 的状态：结论已投进发起方队列、发起方还没推进的空档不在这里，
    由发起方自己的推进判定兜住。
    """
    if background_commands().has_work(task_id) or _running_team_names(task_id):
        return True
    tasks = service_depends.get_task_service()
    for derived in tasks.list_derived_tasks(task_id):
        current = tasks.require_current_run(derived.id)
        if current is not None and current.status in ACTIVE_RUN_STATUSES:
            return True
    return False


def open_derived_titles(task_id: int) -> list[str]:
    """运行中的 Team 与活动派生 Task 的名字（供 ``wait_messages`` 判定与提示）。"""
    tasks = service_depends.get_task_service()
    titles = _running_team_names(task_id)
    for derived in tasks.list_derived_tasks(task_id):
        current = tasks.require_current_run(derived.id)
        if current is not None and current.status in ACTIVE_RUN_STATUSES:
            titles.append(derived.title)
    return titles


def _running_team_names(task_id: int) -> list[str]:
    """主 Task 已批准、还在运行的 Team；待确认的方案是等用户决定，不算下游工作。"""
    return [
        str(row.configuration_snapshot_json["name"])
        for row in AgentTeamRunCrud().list_active()
        if row.parent_task_id == task_id
    ]


def _opened_by_user(run: ConversationRunRecord) -> bool:
    """这个 Run 的第一条输入是不是用户输入；没有输入开头的 Run（后台结论续跑）不算。

    通知不是输入，先跳过（fork 的交接通知排在任务说明前面）。
    """
    for record in ContextStore().run_records(run.task_id, run.id):
        if record.message["role"] != "user":
            continue
        kind = cast(UserMessageMetadata, record.metadata).kind
        if kind == "notice":
            continue
        return kind == "user_input"
    return False


def _report_text(run: ConversationRunRecord) -> str:
    """汇报正文：完成时是它的最终回复，失败时是失败原因（受控文案）。"""
    if run.status == ConversationRunStatus.COMPLETED.value:
        return run.final_output or "(finished without a final reply)"
    error = run.error
    return f"The run failed: {error['message']}" if error else "The run failed."
