"""可观察的终端的共同读取底座：按行读取、条件等待与结果渲染只此一份。

会话（PTY 屏幕）与运行中的命令（管道）只在这三处不同，由子类实现：事实快照（``_facts``）、是否还
在产出（``_is_active``）、一次观察交付后的清理（``_on_delivered``）。子类在构造时设置
``terminal_id``、``_condition``（可重入锁上的 Condition）与 ``_observation``；来源写入后
``notify_all`` 唤醒等待。
"""

from __future__ import annotations

import re
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable

from app.config.constant import Constant
from app.tools.terminal_runtime.errors import TerminalIntegrationRequiredError
from app.tools.terminal_runtime.observation.render import (
    KeepMode,
    LineSearch,
    TerminalFacts,
    TerminalObservationResult,
    render_observation,
)
from app.tools.terminal_runtime.observation.terminal_observation import TerminalObservation
from app.tools.terminal_runtime.observation.wait import TerminalWait, WaitBaseline, WaitEvaluator

RunCancellationCheck = Callable[[], bool]


class ObservedTerminal(ABC):
    """可观察的终端：会话（PTY）与运行中命令（管道）的共同读取底座。"""

    terminal_id: str
    _condition: threading.Condition
    _observation: TerminalObservation
    _last_read: tuple[object, ...] | None

    # ------------------------------------------------------------------ Agent 读取

    def read(
        self,
        *,
        start_line: int | None = None,
        limit: int | None = None,
        tail: int | None = None,
        pattern: str | None = None,
        wait: TerminalWait | None = None,
        is_cancelled: RunCancellationCheck,
    ) -> TerminalObservationResult:
        """按行读取；带 wait 时先按条件等待再读。无变化且无等待的重复读取只给一行提示。

        ``pattern`` 把读取变成搜索：从 ``start_line``（缺省为最早保留行）扫到末行，只返回命中行，
        ``limit`` 限制命中数；搜索总是按逻辑行（全屏程序期间搜冻结的主屏）。正则已在参数边界校验。

        ``is_cancelled`` 要同时认 Task 停止与单次调用的取消：等待在工具线程里持锁进行，只靠它复查
        取消（thread 工具的执行器只在 handler 执行前检查一次）。
        """
        wait_result: dict[str, object] | None = None
        if wait is not None:
            with self._condition:
                integration = self._observation.integration_status
                if wait.until == "command_returned" and integration != "ready":
                    raise TerminalIntegrationRequiredError(
                        "wait until=command_returned needs shell integration, which is "
                        f"{integration} for this terminal"
                    )
                baseline = WaitBaseline.of(self._observation)
            # 没有执行中的命令就没有可等的返回，直接给出空闲结果。
            if wait.until == "command_returned" and baseline.active is None:
                wait_result = {"until": wait.until, "end_reason": "idle"}
            else:
                wait_result = self._wait(WaitEvaluator(wait, baseline), is_cancelled)
        with self._condition:
            facts = self._facts()
            buffer = self._observation.buffer
            mode: KeepMode
            search: LineSearch | None = None
            max_limit = Constant.Terminal.READ_MAX_LIMIT
            if pattern is not None:
                first = start_line or buffer.first_available_line
                last, mode = buffer.last_line, "head"
                search = LineSearch(re.compile(pattern), min(limit or max_limit, max_limit))
            elif start_line is not None:
                count = min(limit or max_limit, max_limit)
                first, last, mode = start_line, start_line + count - 1, "head"
            else:
                count = min(tail or limit or Constant.Terminal.READ_DEFAULT_TAIL, max_limit)
                first, last, mode = buffer.last_line - count + 1, buffer.last_line, "tail"
            logical = start_line is not None or search is not None
            # 全屏程序期间，不指定 start_line 的读取就是读整屏，整屏总在变动区。
            screen_view = buffer.alternate and not logical
            signature = (
                self._observation.information_version,
                facts.status,
                start_line,
                limit,
                tail,
                pattern,
            )
            # 读到当前末行就算触及正在变化的部分：会话的可变区总含光标行；管道输出以换行结尾时可变区
            # 为空，末行已定稿，但轮询末尾同样该被劝去用 wait。只有回看更早历史的区间不计重复。
            touches_live = screen_view or last >= min(buffer.live_from_line, buffer.last_line)
            if (
                wait is None
                and touches_live
                and self._last_read == signature
                and facts.user_marks is None
                and not facts.user_typed_in_screen
            ):
                cursor = (
                    f"cursor s{buffer.screen_cursor_row}"
                    if screen_view
                    else f"cursor line {buffer.cursor_line}"
                )
                return TerminalObservationResult(
                    terminal_id=self.terminal_id,
                    status=facts.status,
                    text=(
                        f"{facts.kind} {self.terminal_id}: no change since your last read "
                        f"({cursor}). Use wait to block until something happens instead of "
                        "reading again."
                    ),
                    screen=screen_view,
                )
            self._last_read = signature if wait is None else None
            return self._build_result(
                first, last, mode, wait_result=wait_result, logical=logical, search=search
            )

    # ------------------------------------------------------------------ 条件等待

    def _wait(
        self, evaluator: WaitEvaluator, is_cancelled: RunCancellationCheck
    ) -> dict[str, object]:
        """不持锁调用：在工具线程里持来源锁等条件（来源写入 ``notify_all`` 唤醒），返回等待结果。

        时间片只为复查取消。来源结束时两种终端同一条规则：先复查一次条件，成立就按条件结算（结束前
        最后一批输出可能已经满足等待；Agent 的命令被停止或超时也确知结束，让 ``command_returned``
        成立），否则以 ``<kind>_ended``（``session_ended`` / ``command_ended``）收尾。会话结束时正在执行
        的命令只是跟丢、不算返回（见 ``CommandStatus``），所以等它的 ``command_returned`` 报
        ``session_ended``。条件名只说等待为什么结束，
        终端的真实结局（退出码、被停止、shell 退出）以表头的 ``ended · <end_reason>`` 为准。
        """
        with self._condition:
            while True:
                if is_cancelled():
                    reason = "cancelled"
                    break
                if not self._is_active():
                    checked, _ = evaluator.check(self._observation, time.monotonic())
                    reason = checked or f"{self._facts().kind}_ended"
                    break
                now = time.monotonic()
                checked, due = evaluator.check(self._observation, now)
                if checked is not None:
                    reason = checked
                    break
                self._condition.wait(
                    min(max(due - now, 0.0), Constant.Terminal.WAIT_CANCEL_CHECK_SECONDS)
                )
            return evaluator.result(reason)

    # ------------------------------------------------------------------ 结果渲染

    def _build_result(
        self,
        first: int,
        last: int,
        mode: KeepMode,
        *,
        wait_result: dict[str, object] | None = None,
        prefilled: str | None = None,
        logical: bool = False,
        search: LineSearch | None = None,
    ) -> TerminalObservationResult:
        """持锁调用：渲染观察文本（屏幕视图 / 逻辑行的选择见 ``render_observation``；区间下限由
        它收拢）；交付后的清理交给子类的 ``_on_delivered``。"""
        result = render_observation(
            self._facts(),
            self._observation,
            first,
            last,
            mode,
            logical=logical,
            search=search,
            wait_result=wait_result,
            prefilled=prefilled,
        )
        self._on_delivered()
        return result

    # ------------------------------------------------------------------ 子类钩子

    @abstractmethod
    def _facts(self) -> TerminalFacts:
        """持锁调用：渲染所需的事实快照。"""

    @abstractmethod
    def _is_active(self) -> bool:
        """持锁调用：来源还在产出（会话 starting / running，命令尚未结束）。"""

    @abstractmethod
    def _on_delivered(self) -> None:
        """持锁调用：一次观察交付之后的清理（会话清空用户输入标注；命令无事可做）。"""
