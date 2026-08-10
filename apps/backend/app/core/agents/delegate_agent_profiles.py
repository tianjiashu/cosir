"""Built-in AgentProfile factories for delegated child work."""

from app.core.agents.agent_profile import AgentProfile


def delegate_reviewer_agent() -> AgentProfile:
    """构建只读的委派审查 Agent profile。

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
        goal="只做代码审查，指出问题、风险和遗漏；不修改代码，不运行测试。",
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
        max_steps=60,
    )


def delegate_analyst_agent() -> AgentProfile:
    """构建只读的委派分析 Agent profile。

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
        goal="只做事实、代码和文档分析，形成结论与建议；不修改代码。",
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
        max_steps=80,
    )


def delegate_coder_agent() -> AgentProfile:
    """构建委派编码 Agent profile。

    参数:
        无。

    返回:
        用于受限代码开发、修复和验证委派的 AgentProfile。

    异常:
        无。

    副作用:
        无。
    """

    return AgentProfile(
        agent_id="delegate_coder",
        role="delegate-coder",
        goal="在父 Agent 委派范围内进行代码开发、修复和验证，并保持改动聚焦、可测试、可审查。",
        allowed_tools=[
            "read_file",
            "list_directory",
            "search_files",
            "write_file",
            "patch",
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
        max_steps=120,
    )
