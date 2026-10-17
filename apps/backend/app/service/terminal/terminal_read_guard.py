"""当前 Run 的只读观察节奏提示；不控制 PTY，不推断命令成功或失败。"""

from typing import Any

from app.service.terminal.errors import TerminalRepeatedReadError


class TerminalReadGuard:
    """由会话锁保护；Task 同时只有一个活 Run，换 Run 即丢弃旧计数。"""

    def __init__(self) -> None:
        self.run_id: int | None = None
        self.previous_version: tuple[int, str] | None = None
        self.repeats = 0

    def observe(self, run_id: int, version: tuple[int, str]) -> dict[str, Any] | None:
        """首次仅建立基线；连续无进展 1～4 次提醒，第 5 次起报可恢复错误。"""
        if self.run_id != run_id:
            self.run_id = run_id
            self.previous_version = None
            self.repeats = 0
        self.repeats = self.repeats + 1 if self.previous_version == version else 0
        self.previous_version = version
        if self.repeats >= 5:
            raise TerminalRepeatedReadError(
                f"Repeated terminal read limited after {self.repeats} consecutive reads with "
                "no new information. This read limit does not stop, fail, or otherwise affect "
                "the command or session."
            )
        if self.repeats:
            return {
                "code": "TERMINAL_READ_UNCHANGED",
                "unchanged_reads": self.repeats,
                "message": (
                    f"Consecutive reads with no new terminal information: {self.repeats}. "
                    "Inspect the existing prompt for required input; if none is needed, "
                    "increase wait_ms before reading again. "
                    "Unchanged output does not mean the command finished."
                ),
            }
        return None
