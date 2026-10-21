"""工具观察 → Transport 终态的映射：状态、短提示与展示数据归一的唯一收口。

同一调用的终态刻意投递两次（至少一次投递 + 消费方幂等），两次都由 :func:`terminal_event` 从同一
份观察构造，值因此由结构保证相同，不靠两处各自拼字段维持：

- **执行即投影（实时）**：``run_tool_batch`` 在每个调用结束时发出，让先跑完的工具先变色，串行
  长命令也不必等整批结束；
- **结算投影（权威）**：``settle_observations`` 写入 ``tool 消息`` 后再发一次，并用同一个事件的
  字段写 metadata。未经执行器的观察（被拦截的批次、被驳回的调用）只有这一次。

消费方按自迁移幂等吸收第二次（含已实时流出终端输出的 part），见对应重放用例。

**终态不得早于进程收尾**：process 工具的取消在检出瞬间只产出观察与日志；``ToolHandlerRunner``
在强杀与输出排空全部收尾后才返回，终态因此只在那之后发出，前端看到的 ``cancelled`` 表达的是
「进程已收尾」。

取消与失败观察不带成功态展示（空 ``dict`` 或只有 ``status_hint``）；已实时流出终端输出的 part
由 Transport 投影（``plan_tool_call_status_changed``）保留实时展示、只叠加终态短提示，不在这里
伪造展示载荷。

本模块只做纯映射，不发事件、不依赖 Transport；展示数据畸形只记日志并降级。
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Literal

from app.config.logging.logger import log
from app.core.events import ToolCallStatusChangedEvent
from app.core.tools.schemas import ToolObservation

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
        display_data: 该观察的展示数据，接受**任意对象**：经 :func:`terminal_event` 传入
            ``ToolObservation.display_data``；结算路径的观察由摘要还原，值可能不是 ``dict``。
            非映射或缺少字符串提示时按失败兜底，不由本函数做形状归一（归一收口在
            :func:`normalize_display_data`）。

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
        无：本函数刻意吞掉深拷贝异常，展示数据畸形不得中断工具链路或结算投影。

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


def terminal_event(
    observation: ToolObservation, *, task_id: int, run_id: int, step_id: str, tool_call_id: str
) -> ToolCallStatusChangedEvent:
    """把一个已归一化的工具观察映射为 ``tool_call_id`` 的终态事件。

    调用身份由调用方显式给出，不读 ``observation.tool_call_id``：各失败工厂未必都回填它。

    实时与结算两条路径都用它构造终态，这是两次投递值相同的唯一保证。实时那次早于 ``tool 消息``
    落 canonical context：快照是进程内 working copy，进程在写 ``tool 消息`` 之前终止时，重建会把
    没有 ``tool 消息`` 行的 part 投影为 ``cancelled``，与「结果丢失且 run 已收敛」的事实一致。

    展示数据不可用不影响终态本身：只跳过展示字段更新，否则一个畸形的 ``display_data`` 会让前端
    一直停在 ``running``。
    """

    status = terminal_status(observation.status)
    return ToolCallStatusChangedEvent(
        task_id=task_id,
        run_id=run_id,
        step_id=step_id,
        tool_call_id=tool_call_id,
        status=status,
        error=terminal_error_hint(status, observation.display_data),
        display_data=normalize_display_data(
            observation.display_data,
            task_id=task_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
        ),
        team_run_id=observation.team_run_id,
    )


__all__ = [
    "ToolTerminalStatus",
    "normalize_display_data",
    "terminal_error_hint",
    "terminal_event",
    "terminal_status",
]
