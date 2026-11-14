"""会话观测页一次查询的结果：全部消息行，加上相关 Run 的状态迁移（运行节点）。

只做读取口径的组装，不改任何事实。观测按 Session + 可选回合进入；Run 对前端不可见，
所以这里不带 Run 号，只带迁移与起止时间。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from openai.types.chat import ChatCompletionMessageFunctionToolCallParam

from app.models.run_record import RunRecord
from app.models.session_context import SessionContextRecord
from app.utils.datetime_utils import from_text


@dataclass(frozen=True)
class ObservedStatusChange:
    """一个运行节点：Run 在 ``at`` 从 ``previous_status`` 到达 ``status``；创建那一条没有来源。

    来源状态不落库（就是历史里的上一条），在这里补上：按回合裁剪后前端看不到上一条。
    """

    at: str
    status: str
    previous_status: str | None
    # 上一个节点的时刻：挂起了多久、停了多久由它算。
    previous_at: str | None
    end_reason: str | None
    # 是否是这个 Run 目前最后一次迁移：Run 的结局与用量挂在它上面展示。
    latest: bool


@dataclass(frozen=True)
class ObservedRun:
    run: RunRecord
    changes: list[ObservedStatusChange]


@dataclass(frozen=True)
class SessionObservation:
    title: str
    # Session 里最大的回合号（不随所选回合变化），前端据此切换回合。
    last_turn: int
    rows: list[SessionContextRecord]
    # 调用 id → 工具名：工具结果行自己不带工具名，而按回合查询时发起调用的那一行可能在更早的回合。
    tool_names: dict[str, str]
    runs: list[ObservedRun]

    @classmethod
    def build(
        cls,
        title: str,
        session_rows: list[SessionContextRecord],
        session_runs: list[RunRecord],
        turn: int | None,
    ) -> SessionObservation:
        """从 Session 的全部行与全部 Run 组装；指定回合时只留这一回合的行与它时间窗内的节点。

        回合不跨 Run，所以一个回合至多对应一个 Run；继承来的回合没有 Run，也就没有节点。
        """

        tool_names = {
            call["id"]: call["function"]["name"]
            for row in session_rows
            if row.message["role"] == "assistant"
            for call in cast(
                list[ChatCompletionMessageFunctionToolCallParam],
                row.message.get("tool_calls") or [],
            )
        }
        runs = [ObservedRun(run, _changes(run)) for run in session_runs]
        last_turn = max((row.turn for row in session_rows), default=0)
        if turn is None:
            return cls(title, last_turn, session_rows, tool_names, runs)

        rows = [row for row in session_rows if row.turn == turn]
        run_id = next((row.run_id for row in rows if row.run_id is not None), None)
        observed = next((item for item in runs if item.run.id == run_id), None)
        if observed is None:
            return cls(title, last_turn, rows, tool_names, [])
        clipped = _clip_to_turn(observed, session_rows, turn)
        return cls(title, last_turn, rows, tool_names, [clipped])


def _changes(run: RunRecord) -> list[ObservedStatusChange]:
    history = run.extra.status_history
    return [
        ObservedStatusChange(
            at=change.at,
            status=change.status,
            previous_status=history[index - 1].status if index else None,
            previous_at=history[index - 1].at if index else None,
            end_reason=change.end_reason,
            latest=index == len(history) - 1,
        )
        for index, change in enumerate(history)
    ]


def _clip_to_turn(
    observed: ObservedRun, session_rows: list[SessionContextRecord], turn: int
) -> ObservedRun:
    """一个 Run 可以有多个回合：只留落在这一回合时间窗里的节点。

    时间窗从这一回合的第一行到下一个回合的第一行；这一回合是 Run 的第一个回合时不设下界
    （「开始」早于第一行），后面没有回合时不设上界。唤醒先于它并入的那条输入落库，所以开启
    新回合的唤醒节点归在上一个回合末尾。
    """

    run_id = observed.run.id
    times = sorted(
        (row.turn, from_text(row.created_at)) for row in session_rows if row.created_at is not None
    )
    first_turn_of_run = min(row.turn for row in session_rows if row.run_id == run_id)
    lower = next((at for row_turn, at in times if row_turn == turn), None)
    upper = next((at for row_turn, at in times if row_turn > turn), None)
    if turn == first_turn_of_run:
        lower = None
    return ObservedRun(
        observed.run,
        [
            change
            for change in observed.changes
            if (lower is None or from_text(change.at) >= lower)
            and (upper is None or from_text(change.at) < upper)
        ],
    )
