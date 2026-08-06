"""进程级工具系统容器。"""

from dataclasses import dataclass

from app.codegraph import CodeGraphKernelClient
from app.config.settings import Settings
from app.tools.guard.tool_output_budget import ToolOutputBudget
from app.tools.tool_execute.tool_scheduler import ToolScheduler
from app.tools.tool_handler.codegraph_query import (
    build_codegraph_callees_definition,
    build_codegraph_callers_definition,
    build_codegraph_explore_definition,
    build_codegraph_impact_definition,
    build_codegraph_node_definition,
    build_codegraph_search_definition,
)
from app.tools.tool_handler.delete import build_delete_definition
from app.tools.tool_handler.execute_terminal import build_execute_terminal_definition
from app.tools.tool_handler.list_directory import build_list_directory_definition
from app.tools.tool_handler.patch_tool import build_patch_definition
from app.tools.tool_handler.read_file import build_read_file_definition
from app.tools.tool_handler.search_files import build_search_files_definition
from app.tools.tool_handler.web_extract import build_web_extract_definition
from app.tools.tool_handler.web_search import build_web_search_definition
from app.tools.tool_handler.write_file import build_write_file_definition
from app.tools.tool_registry import ToolRegistry


@dataclass(frozen=True)
class ToolSystem:
    """进程级工具系统容器（装配与持有）。

    单一职责：把进程内全部内置工具定义注册进统一的 ``ToolRegistry``，并装配
    策略感知的 ``ToolScheduler``，对外暴露「注册表 + 调度器」这一稳定的工具系统
    入口。本类只负责装配与持有，不执行任何工具——执行由 ``ToolScheduler`` 负责。

    参数:
        registry: 进程内所有可用工具定义的注册表。
        scheduler: 运行底座用来列举与执行工具的策略感知调度器。

    返回:
        ToolSystem 实例。

    异常:
        无。

    副作用:
        无（仅持有传入的 registry 与 scheduler，不创建新对象）。
    """

    registry: ToolRegistry
    scheduler: ToolScheduler

    @classmethod
    def build_tool_system(cls, client: CodeGraphKernelClient | None = None) -> "ToolSystem":
        """构建并注册进程级工具系统。

        按内置清单注册全部 15 个工具定义（9 个既有 + 6 个 CodeGraph 查询工具），并用
        ``Settings.MAX_TOOL_OUTPUT_CHARS``（类级静态配置，非传入的 settings 对象）
        构造输出预算上限，装配调度器。

        参数:
            client: 可选的 CodeGraph Kernel RPC 客户端；由调用方（api 装配层）从
                supervisor 取得。为 None 时 CodeGraph 工具仍注册，execute 降级
                （Kernel 不可用）。本方法**不自调** get_client，避免构造即抛破坏装配。

        返回:
            已初始化 registry 与 scheduler 的 ToolSystem。

        异常:
            无（注册过程不抛预期异常）。

        副作用:
            创建内存工具注册表并注册全部内置工具；创建 ToolScheduler 实例。
        """

        registry = ToolRegistry()
        registry.register(build_read_file_definition())
        registry.register(build_write_file_definition())
        registry.register(build_patch_definition())
        registry.register(build_search_files_definition())
        registry.register(build_list_directory_definition())
        registry.register(build_delete_definition())
        registry.register(build_execute_terminal_definition())
        registry.register(build_web_search_definition())
        registry.register(build_web_extract_definition())
        # CodeGraph 查询工具（client 可为 None，execute 降级）。
        registry.register(build_codegraph_explore_definition(client))
        registry.register(build_codegraph_search_definition(client))
        registry.register(build_codegraph_node_definition(client))
        registry.register(build_codegraph_callers_definition(client))
        registry.register(build_codegraph_callees_definition(client))
        registry.register(build_codegraph_impact_definition(client))
        scheduler = ToolScheduler(
            registry=registry,
            output_budget=ToolOutputBudget(Settings.MAX_TOOL_OUTPUT_CHARS),
        )
        return ToolSystem(registry=registry, scheduler=scheduler)
