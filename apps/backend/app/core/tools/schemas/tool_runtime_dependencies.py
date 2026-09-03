"""工具 handler 可用的运行期依赖。"""

import asyncio
from dataclasses import dataclass

from app.core.tools.schemas.delegate_task_executor import DelegateTaskExecutor


@dataclass(frozen=True)
class ToolRuntimeDependencies:
    """承载由 runtime 注入到工具执行上下文的可选能力。"""

    delegate_task_executor: DelegateTaskExecutor | None = None
    runtime_event_loop: asyncio.AbstractEventLoop | None = None
