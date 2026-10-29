"""终端执行引擎包：宿主机 shell 执行后端与执行结果。

本包为工具系统内部引擎，只被同包 ``app.tools.tool_handler.*`` 调用，禁止被
``app.api`` / ``app.core`` / ``app.service`` 跨层直调（与 AGENTS.md 依赖方向契约一致）。
只有本机一种执行后端，调用方直接构造 ``LocalExecutionBackend``，不设抽象基类与工厂。
"""

from app.tools.tool_handler.terminal.execution_result import ExecutionResult
from app.tools.tool_handler.terminal.local_backend import LocalExecutionBackend

__all__ = [
    "ExecutionResult",
    "LocalExecutionBackend",
]
