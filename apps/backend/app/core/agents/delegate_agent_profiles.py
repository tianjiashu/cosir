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
        max_steps=60,
        delegation_type="reviewer",
        capabilities=[
            "只读代码审查",
            "跨文件影响分析",
            "风险与遗漏识别",
        ],
        recommended_use_cases=[
            "审查父 Agent 完成的功能或修复是否引入缺陷",
            "在合并前对 diff 做独立质量把关",
        ],
        constraints=[
            "不修改代码",
            "不运行测试或终端命令",
        ],
        prompt_ref=None,
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
        max_steps=80,
        delegation_type="analyst",
        capabilities=[
            "事实与代码取证",
            "文档与依赖梳理",
            "离线 web 检索",
        ],
        recommended_use_cases=[
            "在不改动代码的前提下澄清一段实现或设计意图",
            "汇总某模块的现状、风险或改进点",
        ],
        constraints=[
            "不修改代码",
            "web 检索仅用于公开资料，不写入凭据",
        ],
        prompt_ref=None,
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
        description=(
            "在父 Agent 委派范围内进行代码开发、修复和验证，"
            "并保持改动聚焦、可测试、可审查。"
        ),
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
        delegation_type="coder",
        capabilities=[
            "代码开发",
            "缺陷修复",
            "自动化验证",
        ],
        recommended_use_cases=[
            "在明确范围内实现某个聚焦功能",
            "修复父 Agent 已定位的缺陷并补充验证",
        ],
        constraints=[
            "改动须聚焦在委派范围",
            "不递归委派子 Agent",
        ],
        prompt_ref=None,
    )
