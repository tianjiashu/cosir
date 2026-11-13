"""一条 ``execute_terminal`` 命令运行期间的观察：可读、可等、可停。

``CommandOutput`` 交来的整理后行写进行缓冲；读取、条件等待与渲染复用 ``ObservedTerminal``，所以
Agent 读用户终端与读自己的命令是同一套参数与输出格式。事实边界：只在命令运行期间存在，结束即从
登记表移除（结论由 ``command_results`` 回投，不归这里）。停止不经这里：经
``command_results`` 的撤销回调落到 ``LocalExecutionBackend.stop``。

线程边界：``feed`` 在 worker 读线程、``finish`` 在作业线程、``read`` 在工具线程调用，全部经
``_condition`` 保护。不自己整理输出：整理器只有一个，在 ``CommandOutput`` 里。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from app.config.constant import Constant
from app.tools.terminal_runtime.command.execution_result import ExecutionResult
from app.tools.terminal_runtime.observation.observed_terminal import ObservedTerminal
from app.tools.terminal_runtime.observation.render import TerminalFacts, render_terminal_summary
from app.tools.terminal_runtime.observation.terminal_observation import TerminalObservation


class RunningCommand(ObservedTerminal):
    """一条命令运行期间的观察对象；由 ``RunningCommandRegistry`` 按 (Session, 调用 id) 持有。

    Session 归属只记在登记表的键上，本对象不重复保存。
    """

    def __init__(
        self,
        *,
        command_id: str,
        command: str,
        shell_kind: str,
        cwd: str,
    ) -> None:
        """构造即进入「命令已开始」状态：观察对象先 ready、再 command_started，与 shell 集成同一词表。"""
        self.terminal_id = command_id
        self._condition = threading.Condition(threading.RLock())
        self._observation = TerminalObservation(shell_kind, "auto")
        self._observation.shell_event({"kind": "ready"})
        self._observation.cwd = cwd
        self._observation.shell_event({"kind": "command_started", "command": command})
        self._result: ExecutionResult | None = None
        self._ended = False
        self._last_read: tuple[object, ...] | None = None

    def feed(self, lines: list[str], pending: str) -> None:
        """收下整理后的完成行与当前未完成行；worker 读线程里调用，不得抛异常。"""
        with self._condition:
            self._observation.pipe_output(lines, pending)
            self._condition.notify_all()

    def finish(self, result: ExecutionResult | None) -> None:
        """落结局并唤醒等待者；只执行一次。``result`` 为 ``None`` 表示执行后端抛了异常、没给出结果
        （worker 没报结局时后端给的是 ``worker_failed``），两者都按结局未知处理。"""
        with self._condition:
            if self._ended:
                return
            if result is None or result.end == "worker_failed":
                # worker 失联：命令可能已经执行、也可能还在，结局未知。
                self._observation.end_tracking("worker_failed")
            elif result.end == "exited":
                self._observation.shell_event(
                    {"kind": "command_returned", "result": {"code": result.exit_code}}
                )
            else:
                # 被停止、超时、被信号结束或没能启动：命令确知已经不在跑。
                self._observation.end_command(result.end)
            self._result = result
            self._ended = True
            self._condition.notify_all()

    def wait_finished(self, timeout_s: float, should_stop: Callable[[], bool]) -> bool:
        """等命令结束：结束返回 ``True``；超时或调用方要求停止返回 ``False``。"""
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while not self._ended:
                if should_stop():
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(min(remaining, Constant.Terminal.WAIT_CANCEL_CHECK_SECONDS))
            return True

    def summary(self) -> str:
        """``terminal_list`` 的一行摘要；与用户会话共用同一份状态措辞。"""
        with self._condition:
            return render_terminal_summary(self._facts(), self._observation)

    # ------------------------------------------------------------------ ObservedTerminal 钩子

    def _facts(self) -> TerminalFacts:
        """持锁调用：渲染所需的命令侧事实。"""
        return TerminalFacts(
            terminal_id=self.terminal_id,
            kind="command",
            status="running" if not self._ended else "ended",
            ended=self._ended,
            end_reason=(
                None
                if not self._ended
                else "worker_failed"
                if self._result is None
                else self._result.end
            ),
            exit_code=None if self._result is None else self._result.exit_code,
            user_typing=False,
            user_marks=None,
            user_typed_in_screen=False,
        )

    def _is_active(self) -> bool:
        """持锁调用：命令尚未结束。"""
        return not self._ended

    def _on_delivered(self) -> None:
        """持锁调用：命令没有用户输入标注，交付后无事可做。"""
