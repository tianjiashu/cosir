"""终端命令执行结果值对象。

本模块只承载 ``ExecutionResult`` 这一个 frozen dataclass，作为执行后端
（``LocalExecutionBackend`` 等）与工具编排层（``ExecuteTerminalTool``）之间的
结构化结果契约。它不含任何 I/O 或业务逻辑。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionResult:
    """一条 shell 命令的归一化执行结果。

    参数:
        output: 合并 stdout+stderr（按到达顺序）后的原始解码文本；ANSI 控制序列原样保留，
            不在执行后端按字符数截断。
        exit_code: 进程退出码；因超时被强杀时为 -1。
        timed_out: 是否因超过命令级超时被强杀。

    返回:
        无。

    异常:
        无。

    副作用:
        无（frozen dataclass，不可变）。
    """

    output: str
    exit_code: int
    timed_out: bool
