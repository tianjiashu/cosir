"""进程级工具系统容器。"""

from dataclasses import dataclass

from app.config.constant import Constant
from app.core.tools.guard.tool_output_budget import ToolOutputBudget
from app.core.tools.tool_execute.tool_executor import ToolExecutor
from app.core.tools.tool_handler.apply_patch_tool import build_apply_patch_definition
from app.core.tools.tool_handler.child_task.child_agent_create import build_delegate_task_definition
from app.core.tools.tool_handler.child_task.child_agent_send import (
    build_child_agent_send_definition,
)
from app.core.tools.tool_handler.child_task.child_agent_status import (
    build_child_agent_status_definition,
)
from app.core.tools.tool_handler.child_task.child_agent_wait import (
    build_child_agent_wait_definition,
)
from app.core.tools.tool_handler.delete_tool import build_delete_file_definition
from app.core.tools.tool_handler.execute_terminal import build_execute_terminal_definition
from app.core.tools.tool_handler.find_files import build_find_files_definition
from app.core.tools.tool_handler.list_directory import build_list_directory_definition
from app.core.tools.tool_handler.move_tool import build_move_file_definition
from app.core.tools.tool_handler.read_file import build_read_file_definition
from app.core.tools.tool_handler.replace_tool import build_replace_definition
from app.core.tools.tool_handler.search_content import build_search_content_definition
from app.core.tools.tool_handler.terminal_session import (
    build_terminal_close_definition,
    build_terminal_read_definition,
    build_terminal_signal_definition,
    build_terminal_start_definition,
    build_terminal_write_definition,
)
from app.core.tools.tool_handler.web_extract import build_web_extract_definition
from app.core.tools.tool_handler.web_search import build_web_search_definition
from app.core.tools.tool_handler.write_file import build_write_file_definition
from app.core.tools.tool_registry import ToolRegistry


@dataclass(frozen=True)
class ToolSystem:
    """进程级工具系统容器（装配与持有）。

    单一职责：把进程内全部内置工具定义注册进统一的 ``ToolRegistry``，并装配
    策略感知的 ``ToolExecutor``，对外暴露「注册表 + 执行器」这一稳定的工具系统
    入口。本类只负责装配与持有，不执行任何工具——执行由 ``ToolExecutor`` 负责。

    参数:
        registry: 进程内所有可用工具定义的注册表。
        executor: 运行底座用来列举与执行工具的策略感知执行管线。

    返回:
        ToolSystem 实例。

    异常:
        无。

    副作用:
        无（仅持有传入的 registry 与 executor，不创建新对象）。
    """

    registry: ToolRegistry
    executor: ToolExecutor

    @classmethod
    def build_tool_system(cls) -> "ToolSystem":
        """构建并注册进程级工具系统。

            按内置清单逐一注册工具定义（包含 ``delegate_task`` 与交互终端工具）；构建函数返回
        ``None`` 的工具（如无可用 Provider 时的 Web 工具）会被注册表跳过，故运行期实际注册数量
        随环境而变，不在 docstring 中固化具体数值。原 patch_write 工具已拆分为
        replace(patch_write) 与 apply_patch(Git unified diff)，另有独立的 delete_file /
        move_file，搜索工具已拆分为 find_files 与 search_content。本方法用
        ``Constant.Tools.MAX_OUTPUT_CHARS``（不可变共享常量，非传入的配置对象）
        构造输出预算上限，装配执行管线（``ToolExecutor``）。工具拦截（Pre/PostToolUse）通过
        ``app.hook.hook_interceptor.HookInterceptor`` 静态方法直接收口，
        无需注入拦截器实例。

        返回:
            已初始化 registry 与 executor 的 ToolSystem。

        异常:
            RuntimeError: Agent registry 尚未在工具系统之前初始化。

        副作用:
            创建内存工具注册表并注册全部内置工具；创建 ToolExecutor 实例。``delegate_task``
            只注册基础定义：候选列表由系统提示词的工具能力目录层下发，目标合法性在执行期按
            workspace 作用域解析。
        """

        # Agent registry 已由 lifespan 在本方法前注入；delegate_task 候选稍后按 Run 投影。
        registry = ToolRegistry()
        registry.register(build_read_file_definition())
        registry.register(build_write_file_definition())
        # patch_write 工具已拆分为 replace(patch_write) 与 apply_patch(Git unified diff)。
        registry.register(build_replace_definition())
        registry.register(build_apply_patch_definition())
        registry.register(build_delete_file_definition())
        registry.register(build_move_file_definition())
        registry.register(build_search_content_definition())
        registry.register(build_find_files_definition())
        registry.register(build_list_directory_definition())
        registry.register(build_execute_terminal_definition())
        registry.register(build_terminal_start_definition())
        registry.register(build_terminal_read_definition())
        registry.register(build_terminal_write_definition())
        registry.register(build_terminal_signal_definition())
        registry.register(build_terminal_close_definition())
        registry.register(build_web_search_definition())
        registry.register(build_web_extract_definition())
        registry.register(build_delegate_task_definition())
        registry.register(build_child_agent_send_definition())
        registry.register(build_child_agent_status_definition())
        registry.register(build_child_agent_wait_definition())
        executor = ToolExecutor(
            registry=registry,
            output_budget=ToolOutputBudget(Constant.Tools.MAX_OUTPUT_CHARS),
        )
        return ToolSystem(registry=registry, executor=executor)
