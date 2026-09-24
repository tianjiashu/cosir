"""工具观察的 Transport 终态投影：状态映射、短提示、展示数据归一与提前投影。

工具执行结束的瞬间就要把终态（``completed`` / ``failed`` / ``cancelled``）推给前端，否则
并行批次里先跑完的工具必须等整批（含最慢的 ``execute_terminal``）结束才变色。本模块是
「工具观察 → Transport 终态事件」的**唯一收口**，被三条路径使用：

- **提前投影**：``ToolExecutor.execute`` 在观察归一化完成后调用
  :func:`project_tool_terminal_state`，使快照先于 canonical context 更新；
- **管线异常兜底**：同处在 ``ToolExecutor.execute`` 捕获到未归一化异常时调用
  :func:`project_unhandled_tool_failure`（此时尚无观察对象）；
- **结算兜底**：``ToolCallLifecycleManager.settle`` 在写入 ``ToolMessage`` 后经
  ``ToolCallStatusChangedEvent`` 再发一次同值终态。兜底不因提前投影而删除，两者对同一调用
  产生相同终态（投影按自迁移幂等吸收）。

映射必须收口在此：三条路径要给出完全相同的 ``status`` 与短错误提示，任何一侧另存一份副本
都会让同一 part 的展示字段随到达顺序漂移。

**终态不得早于进程收尾**：process 路径的取消在检出瞬间只产出观察与日志，**不在执行期投影**；
``ToolHandlerRunner`` 的强杀与输出排空全部收尾之后，才由 ``ToolExecutor.execute`` 的出口把终态
投影进来（thread 路径无子进程收尾，同样由该出口统一投影）。这样前端看到的 ``cancelled`` 表达的
是「进程已收尾」而非「已决定取消」；提前投影还会让强杀期间排空出来的输出增量因
``part.status != "running"`` 被丢弃。

**已知缺陷（未修）**：终态事件会**整体覆盖** part 的 ``display_data``，而取消与失败观察的
``display_data`` 是空 ``dict`` 而非 ``None``（``ToolObservation`` 的字段默认值），因此仍会通过
``ToolCallStatusChangedEvent.plan`` 的 ``is not None`` 守卫，把已流出的终端输出
（``execute_terminal`` 的 ``display_data.output``）清空。修法与归属待用户裁决。

职责边界：
- 负责：观察状态 → 事件终态的映射、终态短提示的选择、展示数据形状归一、把终态直投进进程内快照。
- 不负责：构造模型侧观察（``tool_success`` / ``tool_error`` / ``tool_cancelled``）、写模型
  上下文 ``ToolMessage``（``observe`` 节点）、非法与孤儿调用的补偿收束。

投影是**展示旁路**：任何失败（含展示数据畸形、不可深拷贝）都只记日志并降级，绝不改变工具
观察、不落库、不中断 turn。
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Literal

from app.config.logging.logger import log
from app.core.tools.schemas import ToolObservation
from app.service.depends import get_conversation_event_projector

ToolTerminalStatus = Literal["completed", "failed", "cancelled"]

# 面向前端的终态短提示：与失败工厂写入的 display_data.status_hint 同口径（约 5 字）。
_FAILED_HINT_FALLBACK = "执行失败"
_CANCELLED_HINT = "已取消"


def terminal_status(observation_status: str) -> ToolTerminalStatus:
    """把 ``ToolObservation.status`` 映射为事件契约的终态状态。

    参数:
        observation_status: ``ToolObservation.status``（``success`` / ``cancelled`` /
            其它错误态）。

    返回:
        ``success`` 映射为 ``completed``；``cancelled`` 映射为 ``cancelled``；其余一律
        映射为 ``failed``。

    异常:
        无。

    副作用:
        无（纯函数）。
    """

    if observation_status == "success":
        return "completed"
    if observation_status == "cancelled":
        return "cancelled"
    return "failed"


def terminal_error_hint(event_status: str, display_data: object) -> str | None:
    """选择终态事件携带的短错误提示（完整诊断只留在模型消息里）。

    参数:
        event_status: 事件终态（``completed`` / ``failed`` / ``cancelled``）。
        display_data: 该观察的展示数据，接受**任意对象**：提前投影传入
            ``ToolObservation.display_data``（``dict`` 或 ``None``），结算兜底传入观察摘要的
            ``display_data`` 键（``dataclasses.asdict`` 投影结果）。非映射或缺少字符串提示时
            按失败兜底，不由本函数做形状归一（归一收口在 :func:`normalize_display_data`）。

    返回:
        取消返回「已取消」；失败优先返回 ``display_data["status_hint"]``（后端分类映射产出，
        **含空字符串**，保持与写入值一致），非映射或缺少字符串提示时回退「执行失败」；
        ``completed`` 返回 ``None``。

    异常:
        无。

    副作用:
        无（纯函数，不消费调用方传入的映射）。
    """

    if event_status == "cancelled":
        return _CANCELLED_HINT
    if event_status != "failed":
        return None
    if isinstance(display_data, Mapping):
        hint = display_data.get("status_hint")
        if isinstance(hint, str):
            return hint
    return _FAILED_HINT_FALLBACK


def normalize_display_data(
    value: object,
    *,
    task_id: int | None = None,
    run_id: int | None = None,
    tool_call_id: str = "",
) -> dict[str, object] | None:
    """把任意来源的展示数据归一为事件载荷可用的独立 ``dict``。

    两条路径的 ``display_data`` 形状不同（``ToolObservation.display_data`` 是 dataclass
    字段；结算路径拿到的是 ``dataclasses.asdict`` 投影出的摘要键），本函数是两者**唯一**的
    形状判定、深拷贝与「载荷被丢弃」留痕入口，确保畸形展示数据在两条路径上得到同一种降级、
    且同样可排查（调用方不需要各自补日志，否则两条路径的可观测性会漂移）。

    参数:
        value: 待归一化的展示数据；可以是 ``None``、任意映射或任意其它对象。
        task_id: 丢弃时的定位标识；调用方未知时可不传。
        run_id: 丢弃时的定位标识；调用方未知时可不传。
        tool_call_id: 丢弃时的定位标识；调用方未知时可为空字符串。

    返回:
        与原结构不共享引用的深拷贝 ``dict``；``value`` 不是映射，或深拷贝失败（含不可拷贝
        对象）时返回 ``None``，调用方据此跳过展示字段更新，而不是伪造一个载荷。

    异常:
        无：本函数刻意吞掉深拷贝异常，展示数据畸形不得中断工具链路或结算兜底。

    副作用:
        ``value`` 非 ``None`` 但归一失败时写一条 warning ``tool_display_data_dropped``；只记
        ``display_data_type`` 与定位标识，**不记载荷内容**（避免大体积或敏感渲染数据入日志）。
        ``value`` 为 ``None`` 或归一成功时不写日志。
    """

    if value is None:
        return None
    try:
        normalized: dict[str, object] | None = (
            copy.deepcopy(dict(value)) if isinstance(value, Mapping) else None
        )
    except Exception:
        normalized = None
    if normalized is None:
        log.warning(
            "tool_display_data_dropped",
            extra={
                "msg": "工具展示数据不是可用映射或不可拷贝，已跳过展示字段更新",
                "data": {
                    "task_id": task_id,
                    "run_id": run_id,
                    "tool_call_id": tool_call_id,
                    "display_data_type": type(value).__name__,
                },
            },
        )
    return normalized


def _project(
    *,
    task_id: int,
    run_id: int,
    tool_call_id: str,
    event_status: ToolTerminalStatus,
    error: str | None,
    display_data: dict[str, object] | None,
) -> bool:
    """把已定型的终态字段直投进进程内快照；全部守卫与降级集中在此。

    参数:
        task_id: 本次工具调用所属任务标识。
        run_id: 本次工具调用所属 run 标识。
        tool_call_id: 目标工具调用标识。
        event_status: 已映射的事件终态。
        error: 面向前端的短错误提示；``None`` 表示不改写该字段。
        display_data: 已归一化的展示数据；``None`` 表示不改写该字段。

    返回:
        ``True`` 表示事件已投出；未绑定 run / 缺少身份 / 投影失败时返回 ``False``。

    异常:
        不向上抛出：投影失败只记 error 日志并降级。

    副作用:
        经 projector 更新进程内 snapshot 并通知该 task 的订阅者；成功写 INFO 日志
        ``tool_terminal_state_projected``，跳过写 debug、失败写 error（含堆栈）。
    """

    if task_id <= 0 or run_id <= 0 or not tool_call_id:
        log.debug(
            "tool_terminal_state_projection_skipped",
            extra={
                "msg": "缺少 task/run/工具调用身份，跳过工具终态投影",
                "data": {
                    "task_id": task_id,
                    "run_id": run_id,
                    "tool_call_id": tool_call_id,
                    "status": event_status,
                },
            },
        )
        return False
    try:
        # 延迟导入：``app.assistant_transport.event`` 包初始化会经 tool_names → ``app.core.tools``
        # 包 → tool_system → tool_executor 反向回到本模块，模块级导入会在 event 包尚未导出该事件时
        # 触发 ImportError（后端启动即失败）。投影属运行期路径，此处导入时 event 包已完全就绪。
        from app.assistant_transport.event import ToolCallStatusChangedEvent

        # 直投而非 ``dispatch_conversation_event``：后者在 graph 上下文内优先把事件入 LangGraph
        # custom stream，而 custom stream 要等节点返回才流出；本投影发生在 tools 节点内部的
        # 工具执行期，走 stream 会让「提前」退化成「整批跑完才可见」。
        get_conversation_event_projector().process(
            ToolCallStatusChangedEvent(
                task_id=task_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                status=event_status,
                error=error,
                display_data=display_data,
            )
        )
    except Exception:
        log.exception(
            "tool_terminal_state_projection_failed",
            extra={
                "msg": "工具终态提前投影失败，已降级（不改变工具结果）",
                "data": {
                    "task_id": task_id,
                    "run_id": run_id,
                    "tool_call_id": tool_call_id,
                    "status": event_status,
                },
            },
        )
        return False
    log.info(
        "tool_terminal_state_projected",
        extra={
            "msg": "工具终态已提前投影到进程内快照",
            "data": {
                "task_id": task_id,
                "run_id": run_id,
                "tool_call_id": tool_call_id,
                "status": event_status,
            },
        },
    )
    return True


def project_tool_terminal_state(
    *,
    task_id: int,
    run_id: int,
    tool_call_id: str,
    observation: ToolObservation,
) -> bool:
    """把一个已归一化的工具观察提前投影为进程内快照的工具终态。

    时序：投影刻意**早于** ``ToolMessage`` 落 canonical context（后者由 ``observe`` 节点在
    整批工具结束后写入）。该不一致是允许的：快照是进程内 ephemeral working copy，进程终止后
    由 ``ConversationTaskStateService._rebuild`` 从数据库重建；对没有 ``ToolMessage`` 行的
    tool-call part，重建默认投影为 ``cancelled``
    （``ConversationTaskStateRebuilder.build_pair_tool_part``），与「结果丢失且 run 已收敛」
    的事实一致。代价是进程在写 ``ToolMessage`` 之前终止时，该 part 会从 ``completed`` /
    ``failed`` 回退为 ``cancelled``。

    参数:
        task_id: 本次工具调用所属任务标识。
        run_id: 本次工具调用所属 run 标识；``<= 0`` 表示无 run 绑定（如单测直接调用执行
            管线），跳过投影。
        tool_call_id: 目标工具调用标识；空字符串时跳过投影，避免被事件契约的 ``min_length``
            挡掉后静默丢失终态。
        observation: 执行层返回的已归一化观察；其 ``display_data`` 必须是最终值（执行管线
            的输出预算只截 ``content``，不改 ``display_data``）。

    返回:
        ``True`` 表示终态事件已投出；未绑定 run、缺少调用身份或 projector 失败时返回
        ``False``。**展示数据不可用不影响该返回值**：状态本身仍是有效事实，此时只跳过展示
        字段更新；若把不可用的展示载荷当成投影失败，一个畸形的 ``display_data`` 就会让前端
        一直停在 ``running`` 直到整批工具结束，与本模块的目的相反。

    异常:
        不向上抛出：投影是展示旁路，展示数据畸形、projector 异常都只记日志并降级。

    副作用:
        直接更新进程内 Conversation snapshot 并通知该 task 的 snapshot 订阅者；展示数据不
        可用时由 :func:`normalize_display_data` 写 ``tool_display_data_dropped`` warning。
    """

    event_status = terminal_status(observation.status)
    return _project(
        task_id=task_id,
        run_id=run_id,
        tool_call_id=tool_call_id,
        event_status=event_status,
        error=terminal_error_hint(event_status, observation.display_data),
        display_data=normalize_display_data(
            observation.display_data,
            task_id=task_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
        ),
    )


def project_unhandled_tool_failure(
    *,
    task_id: int,
    run_id: int,
    tool_call_id: str,
) -> bool:
    """把「执行管线自身抛出未归一化异常」的调用投影为 ``failed`` 终态。

    该路径没有 :class:`ToolObservation`（异常发生在观察归一化之前或之外），上游
    ``WorkflowOperations`` 会另造一条内部错误观察交给 ``observe`` 结算。本投影只负责让前端
    立刻看到终态，因此按 ``failed`` 投出、**不携带** ``display_data``（不覆盖已流出的输出、
    也不伪造展示载荷），短提示沿用失败兜底文案。

    参数:
        task_id: 本次工具调用所属任务标识。
        run_id: 本次工具调用所属 run 标识；``<= 0`` 时跳过。
        tool_call_id: 目标工具调用标识；为空时跳过。

    返回:
        ``True`` 表示终态事件已投出；未绑定 run、缺少调用身份或投影失败时返回 ``False``。

    异常:
        不向上抛出（同 :func:`project_tool_terminal_state`）。

    副作用:
        同 :func:`project_tool_terminal_state`。
    """

    return _project(
        task_id=task_id,
        run_id=run_id,
        tool_call_id=tool_call_id,
        event_status="failed",
        error=terminal_error_hint("failed", None),
        display_data=None,
    )


__all__ = [
    "ToolTerminalStatus",
    "normalize_display_data",
    "project_tool_terminal_state",
    "project_unhandled_tool_failure",
    "terminal_error_hint",
    "terminal_status",
]
