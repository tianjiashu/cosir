"""终端执行后端契约（可插拔接缝）。

本模块只定义执行契约（ABC + 签名），不含任何实现。v1 仅交付 ``local``
后端；Docker 等其它后端在 phase-2 以 ``create_backend`` 工厂分支接入，
调用方零改动。
"""

from abc import ABC, abstractmethod

from app.tools.tool_handler.terminal.execution_result import ExecutionResult


class ExecutionBackend(ABC):
    """终端命令执行后端的可插拔契约。

    职责边界：只声明 ``execute`` 签名，不关心危险命令判定、权限校验、
    输出脱敏或 ``ToolObservation`` 组装——这些由上游工具编排层负责。
    """

    @abstractmethod
    def execute(self, command: str, cwd: str, timeout: float) -> ExecutionResult:
        """在宿主机上同步执行一条命令并返回归一化结果。

        参数:
            command: 待执行的 shell 命令字符串。
            cwd: 工作目录（绝对路径）。
            timeout: 命令级超时秒数；超时应尽量回收部分输出并返回
                ``timed_out=True``。

        返回:
            ``ExecutionResult``，含合并输出、退出码、截断与超时标记。

        异常:
            实现方应避免向上抛出；启动失败等异常应转换为带错误文本的
            ``ExecutionResult``（如 ``exit_code=-1``）。

        副作用:
            在宿主机派生进程执行命令；实现须保证超时强杀进程树、不残留孤儿。
        """
