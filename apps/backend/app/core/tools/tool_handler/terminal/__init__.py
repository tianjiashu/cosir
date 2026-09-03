"""终端执行引擎包：灾难级命令裁决 + 可插拔执行后端。

本包为工具系统内部引擎，只被同包 ``app.tools.tool_handler.*`` 调用，禁止被
``app.api`` / ``app.core`` / ``app.service`` 跨层直调（与 AGENTS.md 依赖方向
契约一致）。``create_backend`` 是模块级纯函数工厂，不引入 Manager/Factory 类。
"""

from app.core.tools.tool_handler.terminal.dangerous_command import (
    DangerousCommandVerdict,
    detect_dangerous_command,
)
from app.core.tools.tool_handler.terminal.execution_backend import ExecutionBackend, OutputSink
from app.core.tools.tool_handler.terminal.execution_result import ExecutionResult
from app.core.tools.tool_handler.terminal.local_backend import LocalExecutionBackend


def create_backend(name: str = "local") -> ExecutionBackend:
    """按名称构造执行后端（可插拔接缝，v1 仅 "local"）。

    参数:
        name: 后端名称；v1 仅识别 ``"local"``。

    返回:
        ``ExecutionBackend`` 实例。

    异常:
        ValueError: 当 ``name`` 非 ``"local"`` 时抛出（显式报错优于静默降级）。

    副作用:
        无。
    """
    if name == "local":
        return LocalExecutionBackend()
    raise ValueError(f"unsupported execution backend: {name!r} (v1 only supports 'local')")


__all__ = [
    "DangerousCommandVerdict",
    "ExecutionBackend",
    "ExecutionResult",
    "LocalExecutionBackend",
    "OutputSink",
    "create_backend",
    "detect_dangerous_command",
]
