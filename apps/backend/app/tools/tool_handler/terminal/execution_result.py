"""终端命令执行结果值对象。

本模块只承载 ``ExecutionResult``，作为执行后端（``LocalExecutionBackend``）与工具编排层
（``ExecuteTerminalTool``）之间的结构化结果契约。它不含任何 I/O 或业务逻辑。
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

    ``output`` 是合并 stdout+stderr（按到达顺序）后的原始解码文本，ANSI 控制序列原样保留、不在执行
    后端截断；结局被停止或失败时仍是已收到的部分输出。``exit_code`` 只在 ``exited`` 时有值，
    ``signal`` 只在 ``signaled`` 时有值，``failure`` 只在 ``start_failed`` / ``worker_failed``
    时说明原因。
    """

    end: CommandEnd
    output: str
    exit_code: int | None = None
    signal: int | None = None
    failure: str | None = None
