"""进程级工具系统容器。"""

from dataclasses import dataclass

from app.codegraph import CodeGraphKernelClient
from app.config.settings import Settings
from app.core.tools.guard.tool_output_budget import ToolOutputBudget
from app.core.tools.tool_execute.tool_executor import ToolExecutor
from app.core.tools.tool_handler.apply_patch_tool import build_apply_patch_definition
from app.core.tools.tool_handler.codegraph_query import (
    build_codegraph_callees_definition,
    build_codegraph_callers_definition,
    build_codegraph_explore_definition,
    build_codegraph_impact_definition,
    build_codegraph_node_definition,
    build_codegraph_search_definition,
)
from app.core.tools.tool_handler.delegate_task import build_delegate_task_definition
from app.core.tools.tool_handler.delete import build_delete_definition
from app.core.tools.tool_handler.execute_terminal import build_execute_terminal_definition
from app.core.tools.tool_handler.find_files import build_find_files_definition
from app.core.tools.tool_handler.list_directory import build_list_directory_definition
from app.core.tools.tool_handler.read_file import build_read_file_definition
from app.core.tools.tool_handler.replace_tool import build_replace_definition
from app.core.tools.tool_handler.search_content import build_search_content_definition
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
    def build_tool_system(
        cls,
        client: CodeGraphKernelClient | None = None,
    ) -> "ToolSystem":
        """构建并注册进程级工具系统。

        按内置清单注册工具定义：12 个非 CodeGraph 工具恒注册（包含
        ``delegate_task``）；``Settings.CODEGRAPH_ENABLED`` 为 True 时额外注册 6 个
        CodeGraph 查询工具（共 18 个），为 False 时仅注册 12 个工具（模型侧完全无
        codegraph 入口）。其中原 patch_write 工具已拆分为 replace(patch_write) 与 apply_patch(V4A)，
        搜索工具已拆分为 find_files 与 search_content。本方法用
        ``Settings.MAX_TOOL_OUTPUT_CHARS``（类级静态配置，非传入的 settings 对象）
        构造输出预算上限，装配执行管线（``ToolExecutor``）。工具拦截（Pre/PostToolUse）通过
        ``app.hook.hook_interceptor.HookInterceptor`` 静态方法直接收口，
        无需注入拦截器实例。

        参数:
            client: 可选的 CodeGraph Kernel RPC 客户端；由调用方（api 装配层）从
                supervisor 取得。为 None 时 CodeGraph 工具仍注册，execute 降级
                （Kernel 不可用）。本方法**不自调** get_client，避免构造即抛破坏装配。

        返回:
            已初始化 registry 与 executor 的 ToolSystem。

        异常:
            无（注册过程不抛预期异常；子 Agent 摘要若尚未注入则降级为空串）。

        副作用:
            创建内存工具注册表并注册全部内置工具；创建 ToolExecutor 实例；
            向 ``delegate_task`` 工具描述注入已投影的子 Agent 能力摘要（未注入时降级空串）。
        """

        # configuration.py 也持有 ToolSystem；只能在模块已完成导入后读取
        # registry，不能让 delegate_task handler 在模块级反向导入配置层。
        try:
            from app.config.configuration import get_agent_registry

            delegate_summary = get_agent_registry().child_agent_summary()
        except RuntimeError:
            # 应用首次装配时工具系统先于 agent registry 注入；此时使用通用描述。
            delegate_summary = ""

        registry = ToolRegistry()
        registry.register(build_read_file_definition())
        registry.register(build_write_file_definition())
        # patch_write 工具已拆分为 replace(patch_write) 与 apply_patch(V4A) 两个独立工具
        registry.register(build_replace_definition())
        registry.register(build_apply_patch_definition())
        registry.register(build_search_content_definition())
        registry.register(build_find_files_definition())
        registry.register(build_list_directory_definition())
        registry.register(build_delete_definition())
        registry.register(build_execute_terminal_definition())
        registry.register(build_web_search_definition())
        registry.register(build_web_extract_definition())
        registry.register(build_delegate_task_definition(agent_summary=delegate_summary))
        # CodeGraph 查询工具（client 可为 None，execute 降级）。总开关关闭时不注册，
        # 模型侧完全无 codegraph 工具入口；开关开启时注册 6 个只读查询工具。
        if Settings.CODEGRAPH_ENABLED:
            registry.register(build_codegraph_explore_definition(client))
            registry.register(build_codegraph_search_definition(client))
            registry.register(build_codegraph_node_definition(client))
            registry.register(build_codegraph_callers_definition(client))
            registry.register(build_codegraph_callees_definition(client))
            registry.register(build_codegraph_impact_definition(client))
        executor = ToolExecutor(
            registry=registry,
            output_budget=ToolOutputBudget(Settings.MAX_TOOL_OUTPUT_CHARS),
        )
        return ToolSystem(registry=registry, executor=executor)
