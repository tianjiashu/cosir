"""工具执行上下文值对象。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from app.models.user_decision import UserDecision

if TYPE_CHECKING:
    from app.core.agents.agent_profile import AgentProfile
    from app.core.events import EventSink


@dataclass(frozen=True)
class ToolExecutionContext:
    """一次工具调用所处的运行时边界：任务、工作区根、Run / step 与调用身份。

    由 ``run_tool_batch`` 的单次调用入口按调用构造，一次给齐 Run / step 事实与调用身份；
    ``ToolExecutor`` 及下游直接消费，不再特化。只承载 Run / step / 调用级事实；进程级单例（Agent registry、终端会话服务等）由 handler
    直接取用，不经本对象转手。``agent_profile`` / ``event_loop`` / ``event_sink`` 是父进程
    执行期才有意义的 Run 级对象（不可 pickle 或不应跨进程），process 隔离工具启动子进程前
    必须经 :meth:`for_process_execution` 清空它们。

    Attributes:
        agent_profile: 本次 Run 的 per-run Agent profile；委派与 Agent Team 用它作为
            子节点缺少独立模型配置时的默认值。
        event_loop: 本次 Run 的事件循环。工具在工作线程执行，线程内取不到运行中循环，
            需要回到循环上调度协程（启动子 Run、投递实时输出）时只能用它。
        event_sink: 本次 Run 的执行事件回调；进程工具的实时输出经它投递。
        tool_call_id / user_decision: 单次调用身份，构造时取自该调用；每个调用各一份上下文，
            同批调用不共享。
    """

    task_id: int
    workspace_id: int
    workspace_root: Path
    step_id: str
    run_id: int
    agent_profile: AgentProfile | None = None
    event_loop: asyncio.AbstractEventLoop | None = None
    event_sink: EventSink | None = None
    tool_call_id: str = ""
    user_decision: UserDecision | None = None

    def for_process_execution(self) -> ToolExecutionContext:
        """返回可安全传入隔离子进程的副本：清空父进程专属的 Run 级对象，保留调用身份与决定。"""

        return replace(self, agent_profile=None, event_loop=None, event_sink=None)
