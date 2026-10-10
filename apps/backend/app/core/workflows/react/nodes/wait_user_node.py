"""通用 human-in-the-loop 节点（``wait_user_node``）。

本节点是「工具要求用户先作出决定才能继续」这一关注点的唯一起落点，插在 ``tools`` 与
``observe`` 之间：

1. 从本批观察派生待决请求（判据只有一个：观察上带 ``user_input_request`` 声明，见
   ``user_input_projection``）；没有就直通 ``observe``（不挂起、不发事件、不迁移 Run 状态）；
2. 有请求时先在 :func:`_begin_suspension` 里完成三件副作用：把请求投影给前端 tool part（用户此刻
   才看得见卡片）、把卡片载荷持久化成占位结果行（进程重启后冷重建据此恢复卡片与表单）、把
   Conversation Run 迁移为 ``waiting_for_input``；最后经 LangGraph ``interrupt`` 保存断点，并把
   「需要用户决定什么」作为结构化载荷交给外部驱动方；
3. 恢复时消费唯一请求的结构化决定并**立即物化**：批准把调用重置回「未起跑」并附上用户决定
   （交由 ``tools`` 真正执行），驳回 / 放弃把观察改写为模型可见的取消终态；决定会清掉观察上的
   待决声明（「尚未作答」的唯一标记），随后路由到 ``tools`` 或 ``observe``。

链路契约（不明显但关键）：本节点仅在 ``tools`` 观察带单个待决请求时到达。请求的对外可见性只来自
tool part 的展示载荷，而这次投影由本节点在挂起前完成；若将来在 ``tools`` 与本节点之间插入节点或
改动待决观察的传递，用户会在挂起期间看到空白卡片、也无从作答，且不会有任何报错。

恢复语义（不明显但关键）：

- LangGraph 恢复 interrupt 时**从节点开头重放**，因此 ``mark_waiting_for_input_if_running``
  与待决投影在「消费恢复值」的那一次重放中都不能再执行：前者会让运行中的图与数据库状态分叉
  （该 Run 已由续跑入口原子迁移为 ``running``），后者会重复发一遍同内容事件（幂等但无意义）。
  恢复值一经消费即把 ``resuming_wait_user`` 复位，使后续挂起（含自环重新挂起）重新按「首次
  挂起」处理。
- 恢复值为空时，本节点经**自环**重新挂起同一请求，而不是在同一节点执行内二次 ``interrupt``；
  LangGraph 对已恢复的任务再次 ``interrupt`` 不会挂起图。请求仍由原观察承载，无需另存状态。
- 本节点不创建业务记录、不执行工具、不访问数据库以外的外部系统；待决请求的事实源是 ``tools``
  节点写入的观察。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.types import interrupt

from app.config.logging.logger import log
from app.core.tools.schemas import (
    EXECUTING_DECISION_KINDS,
    UserDecision,
    UserInputRequest,
)
from app.core.workflows.react.node_helper.common import _runtime_config
from app.core.workflows.react.node_helper.user_input_projection import (
    apply_decision_to_observations,
    extract_request,
    parse_resume_decision,
    request_call_id,
)
from app.core.workflows.react.worflow_state.route import ReactRoute
from app.core.workflows.react.worflow_state.state import ReactGraphState

if TYPE_CHECKING:
    from app.core.workflows.react.node_helper.tool_call_lifecycle import (
        ToolCallLifecycleManager,
    )
    from app.core.workflows.react.runtime_config import RuntimeConfig


def _observations(state: ReactGraphState) -> list[dict[str, Any]]:
    """取本模型步的观察摘要列表。

    只做容器形状归一：元素形状由 ``tools`` 节点的 ``dataclasses.asdict`` 投影保证，这里不逐条
    防御——声明形状不合法会在 :func:`extract_request` 处显式上抛，而不是被静默丢掉。

    参数:
        state: 当前 graph state。

    返回:
        本批观察摘要列表；尚未执行过工具时为空列表。

    异常:
        无。

    副作用:
        无。
    """

    observations = state.last_tool_results.get("observations")
    return list(observations) if isinstance(observations, list) else []


def _await_decisions(
    state: ReactGraphState,
    runtime_config: RuntimeConfig,
    request: UserInputRequest,
) -> UserDecision | None:
    """挂起图并返回本次收到的用户决定。

    调用点必须排在「首次挂起的副作用」之后（即 :func:`_begin_suspension` 的投影、持久化与 Run
    迁移）：
    ``interrupt`` 之前的代码在恢复重放时会再跑一遍，而重放不该重复产生那些副作用（由
    ``resuming_wait_user`` 守卫跳过）。恢复值一经消费即复位该标记，使自环后的下一次挂起重新按
    「首次挂起」处理。

    参数:
        state: 当前 graph state；提供 ``step_id`` 供日志定位。
        runtime_config: 运行时配置；提供 Run 身份与 ``resuming_wait_user`` 标记。
        request: 本次挂起的唯一待决请求。

    返回:
        本次恢复的单个领域决定；恢复值为空时返回 ``None``，调用方据此重新挂起该请求。

    异常:
        ValueError: 恢复值结构非法（进程外输入的信任边界，必须显式失败而非静默忽略）。
        LangGraph interrupt 控制流异常: 挂起节点；调用方不得吞掉。

    副作用:
        - 经 LangGraph checkpointer 写入 / 更新工作流断点；
        - 复位 ``resuming_wait_user``；
        - 写 ``wait_user_node_suspended`` / ``wait_user_node_decisions_received`` 结构化日志。
    """

    log.info(
        "wait_user_node_suspended",
        extra={
            "msg": "等待用户决定，已挂起工作流",
            "data": {
                "run_id": runtime_config.run.id,
                "step_id": f"step-{state.step_count}",
                "request_id": request.request_id,
            },
        },
    )
    resume_value = interrupt({
        "kind": "user_input_required",
        "request": request.to_request_payload(),
    })
    # 恢复值已消费：后续再次进入本节点（自环重新挂起、或本轮内又一次提问）都属于新的挂起，
    # 必须重新迁移 Run 状态并重新投影，不能继续沿用「正在消费恢复值」的判定。
    runtime_config.resuming_wait_user = False
    decision = parse_resume_decision(resume_value)
    log.info(
        "wait_user_node_decisions_received",
        extra={
            "msg": "已接收用户决定",
            "data": {
                "run_id": runtime_config.run.id,
                "step_id": f"step-{state.step_count}",
                "decision": (
                    {"request_id": decision.request_id, "decision": decision.kind.value}
                    if decision is not None
                    else None
                ),
            },
        },
    )
    return decision


def _apply_decision(
    state: ReactGraphState,
    runtime_config: RuntimeConfig,
    lifecycle: ToolCallLifecycleManager,
    request: UserInputRequest,
    decision: UserDecision | None,
) -> dict[str, object]:
    """把用户决定物化进调用记录与观察，并给出下一步节点。

    物化是「决定立刻变成下游能读的东西」，写进两个既有通道，不新增任何暂存字段：

    - 批准 → ``tool_call_lifecycle``：把该调用重置回「未起跑」并附上用户决定，``tools`` 据此真正
      执行（决定落在记录上而不是调用参数上，见 ``reopen_for_approved_replay``）；
    - 驳回 / 放弃 → ``last_tool_results``：观察改写为取消终态并带上用户意见，``observe`` 据此写
      模型消息与终态事件；
    - 收到用户决定时：清掉观察上的待决声明——它是「尚未作答」的唯一标记，留着会被下一次派生重新当成
      待决。

    空恢复值自环 ``wait_user``，让 LangGraph 在新的节点执行中重新挂起；有决定时，只要快照中存在
    「已批准但尚未执行」的调用就回 ``tools``，否则交给 ``observe`` 收口。

    参数:
        state: 当前 graph state；取本批观察用于建立「请求 ↔ 调用」关联并改写驳回观察。
        runtime_config: 运行时配置；提供 Run 身份供日志定位。
        lifecycle: 当前工具调用生命周期快照（调用方已校验非空）。
        request: 本次挂起的唯一请求（决定必须指向它，否则即契约分叉）。
        decision: 本次收到的单个用户决定；无决定时为 ``None``。

    返回:
        可直接返回给 LangGraph 的 state 增量：``next_node``（``tools`` / ``observe`` / 自环
        ``wait_user``），以及**仅在确有对应变化时**才写入的 ``tool_call_lifecycle``（存在批准）与
        ``last_tool_results``（有决定命中观察）。

    异常:
        ValueError: 决定指向本批未声明的请求，或使用了该请求未声明的决定种类——进程外输入与工具
            契约已经分叉，必须显式失败；批准落在本轮工具白名单拦下的调用上也在此上抛（批准不能
            扩大权限）。
        KeyError: 批准指向已不存在的调用记录（state 与调用记录分叉）。

    副作用:
        写 ``wait_user_node_resolved`` 结构化日志。
    """

    if decision is not None:
        if decision.request_id != request.request_id:
            raise ValueError(f"决定指向未知的等待请求：{decision.request_id}")
        if not request.accepts(decision.kind):
            raise ValueError(
                f"等待请求 {decision.request_id} 不接受决定 {decision.kind.value}；"
                f"允许：{[kind.value for kind in request.decisions]}"
            )

    observations = _observations(state)
    approved = (
        decision
        if decision is not None and decision.kind in EXECUTING_DECISION_KINDS
        else None
    )
    patch: dict[str, object] = {}
    snapshot = lifecycle
    if approved:
        # 「请求 ↔ 调用」的关联来自观察本身：请求不带 tool_call_id（它是图内部标识）。
        call_id = request_call_id(observations, request.request_id)
        snapshot = lifecycle.reopen_for_approved_replay(
            decisions={call_id: approved}
        )
        patch["tool_call_lifecycle"] = snapshot
    amended = apply_decision_to_observations(observations, decision)
    if amended is not observations:
        patch["last_tool_results"] = {
            **state.last_tool_results,
            "observations": amended,
        }
    # 「已批准但尚未执行」在本快照上就是「未起跑且带用户决定」：``begin`` 会把执行过的迁出
    # pending，settle 会落终态，因此它只命中这条请求刚获批准并等待重执行的调用。
    awaiting_execution = any(
        record.status == "pending" and record.user_decision is not None
        for record in snapshot.valid_tools
    )
    if decision is None:
        next_node = ReactRoute.WAIT_USER
    else:
        next_node = ReactRoute.TOOLS if awaiting_execution else ReactRoute.OBSERVE
    patch["next_node"] = next_node
    log.info(
        "wait_user_node_resolved",
        extra={
            "msg": "用户决定已消费，工作流继续",
            "data": {
                "run_id": runtime_config.run.id,
                "step_id": f"step-{state.step_count}",
                "next_node": next_node.value,
                "decision": decision.kind.value if decision is not None else None,
                "replay_call_id": call_id if approved else None,
                "request_id": request.request_id,
            },
        },
    )
    return patch


def wait_user_node(state: ReactGraphState) -> dict[str, object]:
    """收集用户对工具请求的结构化决定，并决定继续执行还是交给观察节点收口。

    本函数只做编排：派生待决请求 → 无请求即放行 ``observe`` → 有请求则先挂起（首次进入时投影并
    迁移 Run 状态）→ 消费决定 → 物化并路由。四步各自的细节见同文件内的三个辅助函数。

    参数:
        state: 当前 ReAct graph state；观察摘要由 ``tools`` 节点写入 ``last_tool_results``。

    返回:
        需要合并回 graph state 的增量：无待决请求时只写 ``next_node=observe``；有请求时由
        :func:`_apply_decision` 产出最终增量（``next_node`` 与必要的 ``tool_call_lifecycle`` /
        ``last_tool_results``）。

    异常:
        RuntimeError: 存在待决请求却没有 ``tool_call_lifecycle``——请求只能来自已执行调用的结果，
            缺失即 state 已经分叉，静默跳过会退化成「用户作答后什么都没发生」。
        ValueError / KeyError / LangGraph interrupt 控制流异常: 见三个辅助函数的异常段。

    副作用:
        - 挂起前把待决请求投影到前端 tool part，并把 Run 迁移为 ``waiting_for_input``（两者都只在
          非重放进入时执行一次，见 :func:`_await_decisions` 的调用点说明）；
        - 经操作门面把 Conversation Run 迁移为 ``waiting_for_input``（仅在真正挂起前）；
        - 经 LangGraph checkpointer 写入 / 更新工作流断点；
        - 写 ``wait_user_node_*`` 结构化日志。
    """

    request = extract_request(_observations(state))
    if request is None:
        return {"next_node": ReactRoute.OBSERVE}

    runtime_config = _runtime_config()
    lifecycle = state.tool_call_lifecycle
    if lifecycle is None:
        raise RuntimeError("tool_call_lifecycle is required when user input requests exist")

    if not runtime_config.resuming_wait_user:
        _begin_suspension(state, runtime_config, lifecycle)
    decision = _await_decisions(state, runtime_config, request)
    return _apply_decision(state, runtime_config, lifecycle, request, decision)


def _begin_suspension(
    state: ReactGraphState,
    runtime_config: RuntimeConfig,
    lifecycle: ToolCallLifecycleManager,
) -> None:
    """挂起前的三个副作用（顺序固定）：投影 → 持久化待决载荷 → 迁移 Run 为等待态。

    顺序不可颠倒：Run 变成「等待用户输入」之后前端就可能 attach / 刷新，此时卡片内容必须已经在
    快照里、并且已经有一份可冷重建的持久事实。

    为什么这三个都必须是副作用：``interrupt()`` 之前节点不会 ``return``，state patch 不会生效，
    只有事件与数据库写入能留下痕迹——投影走事件流、待决载荷与 Run 状态走数据库。

    参数:
        state: 当前 graph state；取本批观察摘要与 ``step_id``。
        runtime_config: 运行时配置；提供 Run 身份与领域操作门面。
        lifecycle: 当前工具调用生命周期快照（调用方已校验非空）。

    返回:
        无。

    异常:
        KeyError: 待决请求指向的调用不在生命周期快照里（state 已经分叉）。
        RuntimeError / KeyError / TypeError: 不在 graph 运行上下文内调用（取不到 stream writer 或
            运行时上下文）。
        pydantic.ValidationError: 投影事件字段不满足契约。

    副作用:
        - 把待决请求投影到前端 tool part 并持久化成占位结果行
          （:meth:`mark_waiting_for_user_input`）；
        - 把 Conversation Run 迁移为 ``waiting_for_input``；未命中时记
          ``wait_user_node_waiting_transition_missed`` 告警。
    """

    lifecycle.mark_waiting_for_user_input(
        task_id=runtime_config.operations.get_current_task().id,
        run_id=runtime_config.run.id,
        step_id=f"step-{state.step_count}",
        summaries=_observations(state),
    )
    waiting_run = runtime_config.operations.mark_waiting_for_input_if_running()
    if waiting_run is None:
        # 条件更新未命中：Run 已被并发取消 / 落定终态，而图仍要挂起 —— 这正是「图与 Run 状态
        # 分叉」的现场，必须有痕迹可查（否则只会表现为「决定提交后被拒绝」）。
        log.warning(
            "wait_user_node_waiting_transition_missed",
            extra={
                "msg": "Run 已不在 running，无法迁移为等待用户输入，仍按挂起保存断点",
                "data": {
                    "run_id": runtime_config.run.id,
                    "step_id": f"step-{state.step_count}",
                },
            },
        )
