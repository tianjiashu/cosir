"""工具 handler 可用的运行期依赖。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.tools.schemas.tool_output import ProcessToolOutputChannelFactory

if TYPE_CHECKING:
    from app.core.agents.agent_profile import AgentProfile
    from app.core.agents.agent_profile_registry import AgentProfileRegistry
    from app.service.terminal.terminal_session_service import TerminalSessionService


@dataclass(frozen=True)
class ToolRuntimeDependencies:
    """承载由 runtime 注入到工具执行上下文的可选能力。

    ``terminal_session_service`` 只服务同进程工具，类型上仅由 ``TYPE_CHECKING`` 引用以
    避免 tools → service 的运行期导入。输出通道工厂是进程工具实时输出的唯一来源：
    ToolHandlerRunner 在工具执行线程用它建逐调用通道（该线程内取不到运行中循环，
    目标循环取自本依赖的 ``runtime_event_loop``），再把子进程队列中的输出增量接入
    运行期事件；通道不沿执行链层层传参。所有 runtime dependency 都会
    在 ``ToolExecutionContext.for_process_execution`` 中剔除，不进入工具子进程；隔离工具需要的
    配置由该工具在执行边界自行读取，避免把父进程 service 或依赖对象跨进程传递。
    """

    # 委派所需的父 Run 事实：父 Agent 的 per-run profile（child 未显式配置模型时的默认值）
    # 与父 task 是否已处于委派链中（决定单次委派的深度裁决）。
    parent_agent_profile: AgentProfile | None = None
    # 全局 Agent profile Registry；作用域由 ToolExecutionContext.workspace_root 决定。
    agent_profile_registry: AgentProfileRegistry | None = None
    runtime_event_loop: asyncio.AbstractEventLoop | None = None
    process_tool_output_channel_factory: ProcessToolOutputChannelFactory | None = None
    # 仅供同进程 terminal_session handler 使用；process execution 必须清空。
    terminal_session_service: TerminalSessionService | None = None
