from pathlib import Path

from app.config.configuration import get_tool_registry
from app.config.settings import Settings
from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.model_settings import ModelSettings


def _all_tool_names() -> list[str]:
    """调用期读取实时工具注册表全量工具名。"""

    return list(get_tool_registry().get_all_tool_names())


def _codegraph_tool_names() -> list[str]:
    """CodeGraph 启用时返回 6 个查询工具名，关闭时返回空（供 agent 白名单条件裁剪）。"""
    if not Settings.CODEGRAPH_ENABLED:
        return []
    return [
        "codegraph_explore",
        "codegraph_search",
        "codegraph_node",
        "codegraph_callers",
        "codegraph_callees",
        "codegraph_impact",
    ]


def _system_prompt_path(filename: str) -> Path:
    """返回内置 Agent 系统提示词的绝对路径。

    参数:
        filename: 位于 ``app/core/context/system_prompt`` 下的提示词文件名。

    返回:
        基于当前源文件位置解析出的提示词绝对路径，不依赖进程当前工作目录。

    异常:
        无。

    副作用:
        无；仅进行路径拼接，不读取文件。
    """

    return Path(__file__).resolve().parent.parent / "context" / "system_prompt" / filename

# 主 Agent
def main_agent() -> AgentProfile:
    """构建负责理解用户目标、编排工作并汇总结果的主 Agent profile。

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
        allowed_tools=_all_tool_names(),
        agent_type=AgentProfileType.MAIN,
        max_steps=300,
        prompt_file_path=_system_prompt_path("main_agent.md"),
        model_settings=ModelSettings(thinking=True, stream=True, reasoning_effort="high"),
    )

# 代码 reviewer Agent（子 Agent）
def reviewer_agent() -> AgentProfile:
    """构建只读的代码审查 Agent profile。

    该 Agent 面向主 Agent 提供基于证据的代码、变更和设计审查；它只拥有读取、搜索
    与 CodeGraph 查询能力，不承担代码修改、命令执行或测试运行。

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
        agent_type=AgentProfileType.CHILD,
        description=(
            "委派给 delegate_reviewer，用于对指定代码、变更或设计进行只读审查，"
            "重点检查正确性、边界条件、安全性、并发、持久化、契约和测试遗漏。"
            "仅读取和分析，不修改文件、不执行命令、不运行测试；按严重性输出带文件路径、"
            "行号、证据、影响和修复方向的确认问题。"
        ),
        allowed_tools=[
            "read_file",
            "list_directory",
            "search_content",
            "find_files",
            *_codegraph_tool_names(),
        ],
        max_steps=50,
        prompt_file_path=_system_prompt_path("delegate_reviewer.md"),
    )

# 代码 explorer Agent（子 Agent）
def explorer_agent() -> AgentProfile:
    """构建只读的代码分析 Agent profile。

    该 Agent 面向主 Agent 提供本地代码事实、调用链、数据流、架构边界和影响范围；
    外部 Web 资料仅在确有必要时使用，不负责修改代码或运行测试。

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
        agent_id="code-explorer",
        role="code-explorer",
        agent_type=AgentProfileType.CHILD,
        description=(
            "委派给 code-explorer，用于探索不熟悉的代码库，定位入口、调用链、数据流、"
            "配置、契约和改动影响范围。只做只读调查，优先使用本地代码；仅在需要外部官方"
            "文档或用户明确要求时使用 Web。输出应区分已确认事实、推断、证据路径和待确认问题。"
        ),
        allowed_tools=[
            "read_file",
            "list_directory",
            "search_content",
            "find_files",
            "web_search",
            "web_extract",
            *_codegraph_tool_names(),
        ],
        max_steps=80,
        prompt_file_path=_system_prompt_path("code_explorer.md"),
    )

# 代码 test Agent（子 Agent）
def test_agent() -> AgentProfile:
    """构建代码测试 Agent profile。

    该 Agent 可以修改测试文件、fixture 和必要的测试配置并执行验证，但不得修改生产
    代码；发现生产缺陷时应返回可复现证据和建议，而不是替生产代码打补丁。

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
        agent_type=AgentProfileType.CHILD,
        description=(
            "委派给 unit-test-engineer，用于为已有实现补充或改进测试并运行针对性验证，"
            "覆盖正常路径、边界、失败、并发和生命周期行为。可以修改测试文件、fixture 和"
            "必要的测试配置，但不得修改生产代码；发现生产缺陷时返回可复现用例。最终报告"
            "新增测试、执行命令、结果、覆盖盲区和未解决失败。"
        ),
        allowed_tools=_all_tool_names(),
        max_steps=80,
        prompt_file_path=_system_prompt_path("unit_test_engineer.md"),
    )

# 代码 coder Agent（子 Agent）
def coder_agent() -> AgentProfile:
    """构建代码开发 Agent profile（委派子 Agent）。

    该 Agent 面向主 Agent 执行新功能、缺陷修复和结构性重构；它必须遵守 workspace 指令、
    项目架构边界和本地文件安全约束，并在修改后进行与风险相称的验证。

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
        agent_type=AgentProfileType.CHILD,
        description=(
            "委派给 code-developer，用于实现范围明确的新功能、缺陷修复或结构性重构，并"
            "完成相关测试和静态检查。适合已有明确目标、参考路径和验收标准的编码任务，"
            "不适合仅做探索或只读审查。必须遵守 workspace 指令和项目架构边界，避免范围"
            "扩张；完成后报告修改文件、验证命令、结果、风险和未完成事项。"
        ),
        allowed_tools=[
            "read_file",
            "list_directory",
            "search_content",
            "find_files",
            "write_file",
            "patch_write",
            "apply_patch",
            "delete",
            "execute_terminal",
            *_codegraph_tool_names(),
        ],
        max_steps=120,
        prompt_file_path=_system_prompt_path("code_developer.md"),
    )
