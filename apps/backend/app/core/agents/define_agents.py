"""代码维护的主 Agent profile。

主 Agent 的提示词正文外置在 ``main_agent_system_prompt.md``，其余字段由代码决定。内置子 Agent 不在这里：
它们的事实源是系统 ``.cosir/agents`` 下的配置文件，出厂值见 ``service/configuration/agents/builtin_agents``。
"""

from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.model_settings import ModelSettings
from app.core.tool.schemas.tool_names import (
    TOOL_APPLY_PATCH,
    TOOL_ASK_USER,
    TOOL_BROWSER_ACT,
    TOOL_BROWSER_EVALUATE,
    TOOL_BROWSER_LOGS,
    TOOL_BROWSER_NAVIGATE,
    TOOL_BROWSER_SCREENSHOT,
    TOOL_BROWSER_SNAPSHOT,
    TOOL_BROWSER_TABS,
    TOOL_DELEGATE_TASK,
    TOOL_EDIT_FILE,
    TOOL_EXECUTE_TERMINAL,
    TOOL_FIND_FILES,
    TOOL_FORK_SESSION,
    TOOL_OFFER_NEXT_STEP,
    TOOL_PRESENT_DELIVERABLES,
    TOOL_PROPOSE_AGENT_CONFIGURATION,
    TOOL_READ_FILE,
    TOOL_READ_IMAGE,
    TOOL_SEARCH,
    TOOL_SEARCH_CONTENT,
    TOOL_SEND_MESSAGE,
    TOOL_SESSION_STATUS,
    TOOL_SKILL,
    TOOL_TERMINAL_LIST,
    TOOL_TERMINAL_READ,
    TOOL_TERMINAL_STOP,
    TOOL_TERMINAL_WRITE,
    TOOL_WAIT_MESSAGES,
    TOOL_WEB_FETCH,
    TOOL_WEB_SEARCH,
    TOOL_WRITE_FILE,
)


def main_agent(*, system_prompt: str = "", max_steps: int = 300) -> AgentProfile:
    """构建负责理解用户目标、编排工作并汇总结果的主 Agent。

    参数：
        system_prompt: 主 Agent 系统预设正文。由配置装配层从用户配置文件
            ``<system_cosir_dir>/main_agent_system_prompt.md`` 读取后传入；用户尚未配置或读取
            失败时为空串，表示系统提示词不注入 ``<agent_layer>``。
        max_steps: 主 Agent 单轮 workflow 最大步数，由运行时环境配置装配；默认值仅用于直接构造
            profile 的测试与启动装配兜底。

    返回：
        一个新的主 Agent profile，不共享可变运行态。

    异常：
        无（不读取任何内置模板文件，空正文由系统提示词构建层跳过该层）。

    副作用：
        无文件系统访问。
    """

    return AgentProfile(
        agent_id="main_agent",
        role="main_agent",
        allowed_tools=[
            TOOL_READ_FILE,
            TOOL_READ_IMAGE,
            TOOL_WRITE_FILE,
            TOOL_EDIT_FILE,
            TOOL_APPLY_PATCH,
            TOOL_SEARCH_CONTENT,
            TOOL_FIND_FILES,
            TOOL_EXECUTE_TERMINAL,
            TOOL_TERMINAL_LIST,
            TOOL_TERMINAL_READ,
            TOOL_TERMINAL_WRITE,
            TOOL_TERMINAL_STOP,
            TOOL_WEB_SEARCH,
            TOOL_WEB_FETCH,
            TOOL_BROWSER_NAVIGATE,
            TOOL_BROWSER_SNAPSHOT,
            TOOL_BROWSER_ACT,
            TOOL_BROWSER_SCREENSHOT,
            TOOL_BROWSER_TABS,
            TOOL_BROWSER_LOGS,
            TOOL_BROWSER_EVALUATE,
            TOOL_DELEGATE_TASK,
            TOOL_FORK_SESSION,
            TOOL_SEND_MESSAGE,
            TOOL_SESSION_STATUS,
            TOOL_WAIT_MESSAGES,
            TOOL_PROPOSE_AGENT_CONFIGURATION,
            TOOL_ASK_USER,
            TOOL_PRESENT_DELIVERABLES,
            TOOL_OFFER_NEXT_STEP,
            TOOL_SEARCH,
            TOOL_SKILL,
        ],
        agent_type=AgentProfileType.MAIN,
        system_prompt=system_prompt,
        max_steps=max_steps,
        model_settings=ModelSettings(stream=True, reasoning_effort="high"),
    )
