"""内置工具装配：把 Agent 内部工具与外部工具注册进同一个进程级注册表。

装配需要同时引用 ``core/tool`` 的内部工具与 ``app/tools`` 的外部工具，因此放在 ``app/tools``
（``app/tools → core/tool`` 方向合法，core 不得反向导入）。
"""

from app.core.tool.handler.ask_user import build_ask_user_definition
from app.core.tool.handler.offer_next_step import build_offer_next_step_definition
from app.core.tool.handler.tool_search import build_tool_search_definition
from app.core.tool.handler.wait_messages import build_wait_messages_definition
from app.core.tool.handler.web_fetch import build_web_fetch_definition
from app.core.tool.handler.web_search import build_web_search_definition
from app.core.tool.multi_agent.delegate_task import build_delegate_task_definition
from app.core.tool.multi_agent.fork_session import build_fork_session_definition
from app.core.tool.multi_agent.propose_agent_configuration import (
    build_propose_agent_configuration_definition,
)
from app.core.tool.session_management.session_list import build_session_list_definition
from app.core.tool.session_management.session_read import build_session_read_definition
from app.core.tool.session_management.session_send import (
    build_session_send_definition,
)
from app.core.tool.session_management.session_stop import build_session_stop_definition
from app.core.tool.tool_registry import ToolRegistry
from app.tools.tool_handler.browser import build_browser_definitions
from app.tools.tool_handler.file.apply_patch_tool import build_apply_patch_definition
from app.tools.tool_handler.file.edit_file import build_edit_file_definition
from app.tools.tool_handler.file.find_files import build_find_files_definition
from app.tools.tool_handler.file.read_file import build_read_file_definition
from app.tools.tool_handler.file.read_image import ReadImageTool
from app.tools.tool_handler.file.search_content import build_search_content_definition
from app.tools.tool_handler.file.write_file import build_write_file_definition
from app.tools.tool_handler.present_deliverables import build_present_deliverables_definition
from app.tools.tool_handler.skill_tool import build_skill_definition
from app.tools.tool_handler.terminal import build_terminal_definitions
from app.tools.tool_handler.terminal.execute_terminal import build_execute_terminal_definition


def build_tool_registry() -> ToolRegistry:
    """注册全部内置工具定义。web 工具不论 Firecrawl 是否配置都注册：没配置时调用返回配置说明。

    Agent registry 必须先于本函数装配：部分定义构建时读取它。
    """

    # delegate_task 只注册基础定义：候选由系统提示词的工具能力目录层下发，目标合法性在执行期解析。
    registry = ToolRegistry()
    registry.register(build_read_file_definition())
    registry.register(ReadImageTool().to_definition())
    registry.register(build_write_file_definition())
    registry.register(build_edit_file_definition())
    registry.register(build_apply_patch_definition())
    registry.register(build_search_content_definition())
    registry.register(build_find_files_definition())
    registry.register(build_execute_terminal_definition())
    for definition in build_terminal_definitions():
        registry.register(definition)
    registry.register(build_web_search_definition())
    registry.register(build_web_fetch_definition())
    for definition in build_browser_definitions():
        registry.register(definition)
    registry.register(build_delegate_task_definition())
    registry.register(build_fork_session_definition())
    registry.register(build_session_send_definition())
    registry.register(build_session_list_definition())
    registry.register(build_session_read_definition())
    registry.register(build_session_stop_definition())
    registry.register(build_wait_messages_definition())
    registry.register(build_propose_agent_configuration_definition())
    registry.register(build_ask_user_definition())
    registry.register(build_present_deliverables_definition())
    registry.register(build_offer_next_step_definition())
    registry.register(build_skill_definition())
    registry.register(build_tool_search_definition())
    return registry
