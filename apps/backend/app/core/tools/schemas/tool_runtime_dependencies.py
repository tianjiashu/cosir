"""工具 handler 可用的运行期依赖。"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.agents.agent_profile import AgentProfile
from app.core.tools.schemas.tool_output import ProcessToolOutputChannelFactory

if TYPE_CHECKING:
    from app.service.terminal.terminal_session_service import TerminalSessionService


@dataclass(frozen=True)
class ToolRuntimeDependencies:
    """承载由 runtime 注入到工具执行上下文的可选能力。

    ``terminal_session_service`` 只服务同进程工具，类型上仅由 ``TYPE_CHECKING`` 引用以
    避免 tools → service 的运行期导入。输出通道工厂由父进程
    runner 调用，将子进程队列中的输出增量接入运行期事件。所有 runtime dependency 都会
    在 ``ToolExecutionContext.for_process_execution`` 中剔除，不进入工具子进程。
    """

    # 委派所需的父 Run 事实：父 Agent 的 per-run profile（child 未显式配置模型时的默认值）
    # 与父 task 是否已处于委派链中（决定单次委派的深度裁决）。
    parent_agent_profile: AgentProfile | None = None
    parent_task_is_child: bool = False
    runtime_event_loop: asyncio.AbstractEventLoop | None = None
    process_tool_output_channel_factory: ProcessToolOutputChannelFactory | None = None
    # 仅供同进程 terminal_session handler 使用；process execution 必须清空。
    terminal_session_service: "TerminalSessionService | None" = None
    is_run_cancelled: Callable[[int], bool] | None = None
