"""工具执行上下文值对象。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.permission.writable_root import absolute as absolute_writable_root
from app.models.message_context.message_metadata.tool_message_metadata import ToolResultFacts
from app.models.terminal_permissions import TerminalPermissionMode
from app.utils.path.system_cosir import session_scratch_dir

if TYPE_CHECKING:
    from app.core.agents.agent_runtime_config import AgentRuntimeConfig
    from app.core.events import EventSink
    from app.core.tool.schemas.request_tools import RequestTools


def _default_terminal_mode() -> TerminalPermissionMode:
    return "request_approval"


@dataclass(frozen=True)
class ToolExecutionContext:
    """一次工具调用所处的运行时边界：会话、工作区根、Run / step 与调用身份。

    由 ``tools_node`` 为每次调用构造，提供 Run、step 与调用身份。进程级服务由 handler 直接取用。

    Attributes:
        agent: 本次 Run 的 Agent 运行时配置（准入门禁据它判断模型能力）。
        event_loop: 本次 Run 的事件循环。工具在工作线程执行，线程内取不到运行中循环，
            需要回到循环上调度协程（启动子 Run、投递实时输出）时只能用它。
        event_sink: 本次 Run 的执行事件回调；``execute_terminal`` 的实时输出经它投递。
        tool_call_id: 单次调用身份，构造时取自该调用；每个调用各一份上下文，
            同批调用不共享。
    """

    session_id: int
    workspace_id: int
    workspace_root: Path
    step_id: str
    run_id: int
    agent: AgentRuntimeConfig | None = None
    event_loop: asyncio.AbstractEventLoop | None = None
    event_sink: EventSink | None = None
    tool_call_id: str = ""
    # 这次调用所在请求的工具集（``RequestTools``）：``tool_search`` 从中取目录的内置部分与已加载的工具。
    request_tools: RequestTools | None = None
    # Session 的可写根事实（``SessionExtra.writable_root``）；消费方读 :attr:`writable_root`。
    writable_subpath: str | None = None
    # 本次请求模型可见的结果事实（上下文顺序）与本批前面调用刚交出的事实：core 原样交还、不解读，
    # 文件系统据此折叠已读证据（``app/tools/file_system/knowledge``）。
    visible_facts: tuple[ToolResultFacts, ...] = ()
    batch_facts: tuple[ToolResultFacts, ...] = ()
    # 读会话此刻的有效终端权限（``SessionService.effective_terminal_mode``）：用户随时可切，所以不在构造时取，
    # 由真正启动命令的那一刻读（:attr:`terminal_permission_mode`），其余工具从不触发这次读取。
    read_terminal_mode: Callable[[], TerminalPermissionMode] = _default_terminal_mode

    @property
    def terminal_permission_mode(self) -> TerminalPermissionMode:
        return self.read_terminal_mode()

    @property
    def writable_root(self) -> Path:
        """写类文件工具的边界与终端的缺省工作目录；未收窄时就是工作区根。"""

        return absolute_writable_root(self.workspace_root, self.writable_subpath)

    @property
    def scratch_dir(self) -> Path:
        """本会话的临时目录：写类文件工具的第二个合法写入区，终端经 ``COSIR_SCRATCH_DIR`` 拿到它。

        只由会话 id 推导（``session_scratch_dir``），不随可写根收窄：收窄的子 Agent 照样需要放临时文件。
        """

        return session_scratch_dir(self.session_id)
