from pathlib import Path

from app.config.configuration import get_tool_registry
from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.model_settings import ModelSettings


def _all_tool_names() -> list[str]:
    """调用期读取实时工具注册表全量工具名。"""

    return list(get_tool_registry().get_all_tool_names())


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


# 子 Agent 的 description 契约（面向父 Agent 的「选择指南」，不是执行协议）：
# - 读者只有父 Agent（经 child_agent_summary 投影进 delegate_task 工具描述），故统一英文；
# - 只写「何时该选它 / 何时不该选它（含该改选谁）/ 硬边界」，每条 ≤ 260 字符；
# - **不写**：agent_id（清单已渲染）、工具清单（由 allowed_tools 表达）、输出格式与报告字段、
#   以及「必须遵守 workspace 指令」这类对所有子 Agent 都成立的通用纪律——它们分别属于
#   prompt_file_path 里的执行协议与 delegate_task.message 里的具体工作单。


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

    该 Agent 面向主 Agent 提供基于证据的代码、变更和设计审查；它只拥有读取与搜索
    能力，不承担代码修改、命令执行或测试运行。

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
            "Read-only review of a given change or design: correctness, edge cases, security, "
            "concurrency, contracts, test gaps. Use for: critiquing a concrete diff. Not for: "
            "open-ended exploration (use code-explorer). Limits: no file edits, no commands, "
            "no test runs."
        ),
        allowed_tools=[
            "read_file",
            "list_directory",
            "search_content",
            "find_files",
        ],
        max_steps=100,
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
            "Read-only exploration of unfamiliar code: entry points, call chains, data flow, "
            'config, contracts, blast radius. Use for: "how/where does X work". Not for: '
            "critiquing a known diff (use delegate_reviewer) or edits. Local code first; web "
            "only if needed."
        ),
        allowed_tools=[
            "read_file",
            "list_directory",
            "search_content",
            "find_files",
            "web_search",
            "web_extract",
        ],
        max_steps=100,
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
            "Adds or improves tests and runs scoped verification: happy path, edge cases, "
            "failures, concurrency. Use for: covering existing code, or reproducing a suspected "
            "bug. Limits: edits test files, fixtures and test config only; never production code."
        ),
        allowed_tools=_all_tool_names(),
        max_steps=100,
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
            "Implements well-scoped features, bug fixes or refactors, with their tests and "
            "static checks. Use for: tasks whose goal, reference paths and acceptance criteria "
            "are clear. Not for: exploration or review. Can edit files and run commands."
        ),
        allowed_tools=[
            "read_file",
            "list_directory",
            "search_content",
            "find_files",
            "write_file",
            "patch_write",
            "apply_patch",
            "delete_file",
            "move_file",
            "execute_terminal",
            "terminal_start",
            "terminal_read",
            "terminal_write",
            "terminal_signal",
            "terminal_close",
        ],
        max_steps=120,
        prompt_file_path=_system_prompt_path("code_developer.md"),
    )
