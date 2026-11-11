"""终端命令执行结果值对象。

本模块只承载 ``ExecutionResult``，作为执行后端（``LocalExecutionBackend``）与工具编排层
（``ExecuteTerminalTool``）之间的结构化结果契约。它只表达结局，不含命令输出（输出归
``CommandOutput``），也不含任何 I/O 或业务逻辑。
"""

from dataclasses import dataclass
from typing import Literal

# 命令怎样结束——结局的唯一事实，观察状态、给模型的标记、后台结论与展示数据都只从它派生：
# - ``exited`` / ``signaled``：命令自己结束（含非零退出与被外部信号结束），是命令的结果；
# - ``timed_out`` / ``cancelled``：后端要求停止，且先于命令自己结束生效（worker 裁决先后）；
# - ``start_failed``：命令没有启动，什么也没执行；
# - ``worker_failed``：worker 失联或自身故障，命令可能已经执行，结果未知。
CommandEnd = Literal[
    "exited", "signaled", "timed_out", "cancelled", "start_failed", "worker_failed"
]


@dataclass(frozen=True)
class ExecutionResult:
    """一条 shell 命令的归一化执行结果。

    ``exit_code`` 只在 ``exited`` 时有值，``signal`` 只在 ``signaled`` 时有值，``failure`` 只在
    ``start_failed`` / ``worker_failed`` 时说明原因。命令输出由调用方的 ``CommandOutput`` 收集，
    本对象不携带。
    """

    end: CommandEnd
    exit_code: int | None = None
    signal: int | None = None
    failure: str | None = None

    def end_marker(self) -> str:
        """结局标记（``[exit_code=N]`` 等）：前台模型正文与后台结论共用同一张表。"""
        return {
            "exited": f"[exit_code={self.exit_code}]",
            "signaled": f"[signal={self.signal}]",
            "timed_out": "[timed_out]",
            "cancelled": "[cancelled]",
            "start_failed": "[start_failed]",
            "worker_failed": "[outcome_unknown]",
        }[self.end]
