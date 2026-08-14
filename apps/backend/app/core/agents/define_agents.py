from app.config.configuration import get_tool_registry
from app.core.agents.agent_profile import AgentProfile
from app.core.llm.model_settings import ModelSettings

# 默认 Agent 标识：前端未显式选择 agent 时回退到该内置 developer。
DEFAULT_AGENT_ID = "developer"

# 内置默认工具名回退集合：当工具系统单例尚未初始化（如测试导入期）时，
# 保证 developer 系父 profile 仍具备完整工具可见性，避免模块导入期触碰
# 未初始化的全局单例而崩溃。生产启动时工具系统已注入，优先以注册表为准。
_DEFAULT_DEVELOPER_TOOLS = (
    "read_file",
    "list_directory",
    "search_files",
    "write_file",
    "patch",
    "apply_patch",
    "delete",
    "execute_terminal",
    "delegate_task",
    "codegraph_explore",
    "codegraph_search",
    "codegraph_node",
    "codegraph_callers",
    "codegraph_callees",
    "codegraph_impact",
)


def _resolve_all_tool_names() -> list[str]:
    """解析 developer 系父 profile 的默认工具名清单（延迟求值，打破导入期单例依赖）。

    优先返回进程级工具系统注册表中的全部工具名；当工具系统单例尚未初始化
    （如测试直接导入本模块构造 profile）时，回退到内置默认工具名集合，
    保证 ``allowed_tools`` 始终非空且含 ``delegate_task``，不依赖全局单例就绪顺序。

    参数:
        无。

    返回:
        工具名称列表（顺序稳定，来自工具系统或内置回退集合）。

    异常:
        无。

    副作用:
        无（只读工具系统单例，不修改任何状态）。
    """

    tool_system = None
    try:
        tool_system = get_tool_registry()
    except Exception:
        tool_system = None

    if tool_system is None:
        return list(_DEFAULT_DEVELOPER_TOOLS)
    return tool_system.get_all_tool_names()


def developer_agent() -> AgentProfile:
    """构建第一版默认的开发者 Agent profile。

    参数:
        无。

    返回:
        用于内置单 Agent 的 AgentProfile。

    异常:
        无。

    副作用:
        无。
    """

    return AgentProfile(
        agent_id="developer",
        role="developer_agent",
        description="协助用户完成软件工程项目开发任务",
        allowed_tools=_resolve_all_tool_names(),
        context_policy="text_only_v1",
        model_name="deepseek-v4-flash",
        model_settings=ModelSettings(
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
        ),
        max_steps=300,
        prompt_ref=None,
    )


# 与重构前 ``agent_profile.default_developer_agent`` 保持名称兼容的薄别名，
# 供测试与历史调用方继续以 ``default_developer_agent()`` 取默认父 profile。
default_developer_agent = developer_agent


def developer_agent_pro() -> AgentProfile:
    """构建开发者 Agent profile Pro。

    参数:
        无。

    返回:
        用于内置单 Agent 的 AgentProfile。

    异常:
        无。

    副作用:
        无。
    """

    return AgentProfile(
        agent_id="developer_pro",
        role="coding-agent-pro",
        description="协助用户完成软件工程项目开发任务",
        allowed_tools=_resolve_all_tool_names(),
        context_policy="text_only_v1",
        model_name="deepseek-v4-pro",
        model_settings=ModelSettings(
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
        ),
        max_steps=300,
        prompt_ref=None,
    )



def reviewer_agent() -> AgentProfile:
    """构建只读的代码审查 Agent profile。

    参数:
        无。

    返回:
        用于代码审查委派的 AgentProfile。

    异常:
        无。

    副作用:
        无。
    """

    return AgentProfile(
        agent_id="delegate_reviewer",
        role="delegate-reviewer",
        description="只做代码审查，指出问题、风险和遗漏；不修改代码，不运行测试。",
        allowed_tools=[
            "read_file",
            "list_directory",
            "search_files",
            "codegraph_explore",
            "codegraph_search",
            "codegraph_node",
            "codegraph_callers",
            "codegraph_callees",
            "codegraph_impact",
        ],
        context_policy="text_only_v1",
        model_name="deepseek-v4-flash",
        model_settings=ModelSettings(
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
        ),
        max_steps=60,
        prompt_ref=None,
    )


def analyst_agent() -> AgentProfile:
    """构建只读的代码分析 Agent profile。

    参数:
        无。

    返回:
        用于事实、代码和文档分析委派的 AgentProfile。

    异常:
        无。

    副作用:
        无。
    """

    return AgentProfile(
        agent_id="delegate_analyst",
        role="delegate-analyst",
        description="只做事实、代码和文档分析，形成结论与建议；不修改代码。",
        allowed_tools=[
            "read_file",
            "list_directory",
            "search_files",
            "web_search",
            "web_extract",
            "codegraph_explore",
            "codegraph_search",
            "codegraph_node",
            "codegraph_callers",
            "codegraph_callees",
            "codegraph_impact",
        ],
        context_policy="text_only_v1",
        model_name="deepseek-v4-flash",
        model_settings=ModelSettings(
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
        ),
        max_steps=80,
        prompt_ref=None,
    )

def test_agent() -> AgentProfile:
    """构建代码测试 Agent profile。

    参数:
        无。

    返回:
        用于代码测试委派的 AgentProfile。

    异常:
        无。

    副作用:
        无。
    """
    return AgentProfile(
        agent_id="delegate_tester",
        role="delegate-tester",
        description="只做代码测试，指出问题、风险和遗漏；不修改代码，不运行测试。",
        allowed_tools=_resolve_all_tool_names(),
        context_policy="text_only_v1",
        model_name="deepseek-v4-flash",
        model_settings=ModelSettings(
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
        ),
        max_steps=80,
        prompt_ref=None,
    )


def coder_agent() -> AgentProfile:
    """构建代码开发 Agent profile（委派子 Agent）。

    参数:
        无。

    返回:
        用于代码开发委派的 AgentProfile。

    异常:
        无。

    副作用:
        无。
    """
    return AgentProfile(
        agent_id="delegate_coder",
        role="delegate-coder",
        description=(
            "在父 Agent 委派范围内进行代码开发、修复和验证，并保持改动聚焦、"
            "可测试、可审查。"
        ),
        allowed_tools=[
            "read_file",
            "list_directory",
            "search_files",
            "write_file",
            "patch",
            "apply_patch",
            "delete",
            "execute_terminal",
            "codegraph_explore",
            "codegraph_search",
            "codegraph_node",
            "codegraph_callers",
            "codegraph_callees",
            "codegraph_impact",
        ],
        context_policy="text_only_v1",
        model_name="deepseek-v4-flash",
        model_settings=ModelSettings(
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
        ),
        max_steps=120,
        prompt_ref=None,
    )
