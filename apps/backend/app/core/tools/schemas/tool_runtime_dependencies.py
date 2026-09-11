"""工具 handler 可用的运行期依赖。"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.tools.schemas.delegate_task_executor import DelegateTaskExecutor

if TYPE_CHECKING:
    from app.service.terminal.terminal_session_service import TerminalSessionService


@dataclass(frozen=True)
class ToolRuntimeDependencies:
    """承载由 runtime 注入到工具执行上下文的可选能力。

    terminal service 和取消回调只允许同进程 thread handler 使用；
    ``ToolExecutionContext.for_process_execution`` 会构造不携带这两个引用的副本。
    """

    delegate_task_executor: DelegateTaskExecutor | None = None
    runtime_event_loop: asyncio.AbstractEventLoop | None = None
    # 仅供同进程 terminal_session handler 使用；process execution 必须清空。
    terminal_session_service: "TerminalSessionService | None" = None
    is_run_cancelled: Callable[[int], bool] | None = None
