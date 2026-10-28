"""内置工具装配：把 Agent 内部工具与外部工具注册进同一个进程级注册表。

装配需要同时引用 ``core/tool`` 的内部工具与 ``app/tools`` 的外部工具，因此放在 ``app/tools``
（``app/tools → core/tool`` 方向合法，core 不得反向导入）。
"""

from app.core.tool.handler.agent_team_run import build_agent_team_run_definition
from app.core.tool.handler.propose_agent_team_configuration import (
    build_propose_agent_team_configuration_definition,
)
from app.tools.tool_handler.apply_patch_tool import build_apply_patch_definition
from app.core.tool.handler.child_agent_create import build_delegate_task_definition
from app.core.tool.handler.child_agent_send import (
    build_child_agent_send_definition,
)
from app.core.tool.handler.child_agent_status import (
    build_child_agent_status_definition,
)
from app.core.tool.handler.propose_agent_configuration import (
    build_propose_agent_configuration_definition,
)
from app.tools.tool_handler.delete_tool import build_delete_file_definition
from app.tools.tool_handler.execute_terminal import build_execute_terminal_definition
from app.tools.tool_handler.find_files import build_find_files_definition
from app.tools.tool_handler.list_directory import build_list_directory_definition
from app.core.tool.handler.mcp_tool import build_mcp_definition
from app.tools.tool_handler.move_tool import build_move_file_definition
from app.tools.tool_handler.read_file import build_read_file_definition
from app.tools.tool_handler.replace_tool import build_replace_definition
from app.tools.tool_handler.search_content import build_search_content_definition
from app.core.tool.handler.skill_tool import build_skill_definition
from app.tools.tool_handler.terminal_session import build_terminal_definitions
from app.core.tool.handler.wait_messages import build_wait_messages_definition
from app.core.tool.handler.web_extract import build_web_extract_definition
from app.core.tool.handler.web_search import build_web_search_definition
from app.tools.tool_handler.write_file import build_write_file_definition
from app.core.tool.tool_registry import ToolRegistry


def build_tool_registry() -> ToolRegistry:
    """注册全部内置工具定义；构建函数返回 ``None`` 的工具（如无可用 Provider 的 Web 工具）由注册表跳过。

    Agent registry 必须先于本函数装配：部分定义构建时读取它。
    """

    # delegate_task 只注册基础定义：候选由系统提示词的工具能力目录层下发，目标合法性在执行期解析。
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
    for definition in build_terminal_definitions():
        registry.register(definition)
    registry.register(build_web_search_definition())
    registry.register(build_web_extract_definition())
    registry.register(build_delegate_task_definition())
    registry.register(build_child_agent_send_definition())
    registry.register(build_child_agent_status_definition())
    registry.register(build_wait_messages_definition())
    registry.register(build_propose_agent_configuration_definition())
    # Team 工具统一注册；是否对当前 Agent 开放由工具选择与 Run 授权决定。
    registry.register(build_propose_agent_team_configuration_definition())
    registry.register(build_agent_team_run_definition())
    registry.register(build_mcp_definition())
    registry.register(build_skill_definition())
    return registry
