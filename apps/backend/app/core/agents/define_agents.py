from app.config.configuration import get_tool_registry
from app.core.agents.agent_profile import AgentProfile
from app.core.agents.model_settings import ModelSettings

# 内置默认工具名回退集合：当工具系统单例尚未初始化（如测试导入期）时，
# 保证 developer 系父 profile 仍具备完整工具可见性，避免模块导入期触碰
# 未初始化的全局单例而崩溃。生产启动时工具系统已注入，优先以注册表为准。
_ALL_TOOLS = get_tool_registry().get_all_tool_names()


def main_agent() -> AgentProfile:
    """

    参数:
        无。

    返回:
        用于主 Agent 的 AgentProfile；其他 profile 是供主 Agent 使用的子 Agent，
        或隐藏在项目内部的 Agent（如上下文压缩）。

    异常:
        无。

    副作用:
        无。
    """

    return AgentProfile(
        agent_id="main_agent",
        role="main_agent",
        description="协助用户完成软件工程项目开发任务",
        allowed_tools=_ALL_TOOLS,
        max_steps=300,
        main_agent=True,
        prompt_file_path=None,
        model_settings=ModelSettings(thinking=True, stream=True, reasoning_effort="high"),
    )


# 与重构前 ``agent_profile.default_developer_agent`` 保持名称兼容的薄别名，
# 供测试与历史调用方继续以 ``default_developer_agent()`` 取默认父 profile。
default_developer_agent = main_agent


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
        hidden=True,
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
        # 子Agent需要指定模型，否则会报错，提示未配置模型
        model_name="deepseek/deepseek-v4-flash",
        max_steps=50,
        prompt_file_path=None,
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
        agent_id="code-spec-reviewer",
        role="code-spec-reviewer",
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
        model_name="deepseek/deepseek-v4-flash",
        max_steps=80,
        prompt_file_path=None,
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
        agent_id="unit-test-engineer",
        role="unit-test-engineer",
        description=(
            """
            Use this agent when you need to write comprehensive unit tests for existing
            production code.
            This agent strictly focuses on testing only and will not modify any production code.
            <example>
              Context: User has written a TypeScript utility function and needs comprehensive
              unit tests.
              user: "Please write unit tests for src/utils/validation.ts"
              assistant: "I'll use the unit-test-engineer agent to write comprehensive unit
              tests for this file"
            </example>
            <example>
              Context: Developer wants to check if existing code has sufficient test coverage
              and potential bugs.
              user: "Test the new authentication module to find any concurrency or boundary issues"
              assistant: "Launching unit-test-engineer to perform adversarial testing on the
              authentication module"
            </example>
            """
        ),
        allowed_tools=_ALL_TOOLS,
        model_name="deepseek/deepseek-v4-flash",
        max_steps=80,
        prompt_file_path=None,
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
        agent_id="code-developer",
        role="code-developer",
        description=(
            "Use this agent when you need to implement new features, refactor existing code, "
            "or add functionality to a project."
            "This agent follows strict coding standards focused on long-term maintainability."
            "This agent prioritizes structural improvements and mature dependency reuse over "
            "minimal, "
            "patch-style changes when they benefit long-term iteration."
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
        model_name="deepseek/deepseek-v4-flash",
        max_steps=120,
        prompt_file_path=None,
    )
