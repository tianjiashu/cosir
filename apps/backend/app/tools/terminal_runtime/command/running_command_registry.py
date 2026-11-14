"""运行中命令的进程内登记表：按 (session_id, command_id) 持有 ``RunningCommand``。

与 ``TerminalSessionService`` 对称：单个对象在 ``running_command``，集合在这里。命令一定会结束
（超时、取消、Run 停止、Session 删除、后端关闭都会让作业收尾），所以登记表不需要另外清理；不持久化、
不跨重启。
"""

from __future__ import annotations

import threading

from app.tools.terminal_runtime.command.execution_result import ExecutionResult
from app.tools.terminal_runtime.command.running_command import RunningCommand


class RunningCommandRegistry:
    """按 (Session, 调用 id) 持有运行中的命令；线程安全，任意线程可调用。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._commands: dict[tuple[int, str], RunningCommand] = {}

    def start(
        self,
        *,
        session_id: int,
        command_id: str,
        command: str,
        shell_kind: str,
        cwd: str,
    ) -> RunningCommand:
        """登记一条刚开始运行的命令。

        异常:
            RuntimeError: 同一 Session 同一调用 id 已有条目。``tool_call_id`` 在 Session 内唯一，出现即
                代码缺陷，不静默覆盖。
        """
        with self._lock:
            key = (session_id, command_id)
            if key in self._commands:
                raise RuntimeError(f"running command {command_id} is already registered")
            running = RunningCommand(
                command_id=command_id,
                command=command,
                shell_kind=shell_kind,
                cwd=cwd,
            )
            self._commands[key] = running
        return running

    def get(self, session_id: int, command_id: str) -> RunningCommand:
        """按 Session 归属取运行中的命令；不存在抛 ``KeyError``。"""
        with self._lock:
            running = self._commands.get((session_id, command_id))
        if running is None:
            raise KeyError(f"no running command {command_id} in session {session_id}")
        return running

    def session_commands(self, session_id: int) -> list[RunningCommand]:
        """本 Session 运行中的命令，按开始先后。"""
        with self._lock:
            return [
                running
                for (owner, _), running in self._commands.items()
                if owner == session_id
            ]

    def finish(
        self, session_id: int, command_id: str, result: ExecutionResult | None
    ) -> None:
        """落结局（唤醒正在等待的读取方）后移除条目；重复调用无副作用。"""
        with self._lock:
            running = self._commands.get((session_id, command_id))
        if running is None:
            return
        running.finish(result)
        with self._lock:
            self._commands.pop((session_id, command_id), None)


running_commands = RunningCommandRegistry()
