"""工具 handler 可用的运行期依赖。"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.tools.schemas.delegate_task_executor import DelegateTaskExecutor
from app.core.tools.schemas.tool_output import ProcessToolOutputChannelFactory

if TYPE_CHECKING:
    from app.service.task.change_set.file_mutation_service import FileMutationService
    from app.service.terminal.terminal_session_service import TerminalSessionService


@dataclass(frozen=True)
class ToolRuntimeDependencies:
    """承载由 runtime 注入到工具执行上下文的可选能力。

    ``terminal_session_service`` 与 ``file_mutation_service`` 都只服务同进程工具
    （分别供 terminal session handler 与结构化文件工具使用），类型上仅由
    ``TYPE_CHECKING`` 引用以避免 tools → service 的运行期导入。输出通道工厂由父进程
    runner 调用，将子进程队列中的输出增量接入运行期事件。所有 runtime dependency 都会
    在 ``ToolExecutionContext.for_process_execution`` 中剔除，不进入工具子进程。
    """

    delegate_task_executor: DelegateTaskExecutor | None = None
    runtime_event_loop: asyncio.AbstractEventLoop | None = None
    process_tool_output_channel_factory: ProcessToolOutputChannelFactory | None = None
    # 仅供同进程 terminal_session handler 使用；process execution 必须清空。
    terminal_session_service: "TerminalSessionService | None" = None
    is_run_cancelled: Callable[[int], bool] | None = None
    # 仅供同进程文件工具使用：由 runtime 注入的持久化写入协调器；process execution 必须清空。
    file_mutation_service: "FileMutationService | None" = None
