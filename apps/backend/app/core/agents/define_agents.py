"""定义必须保留在代码中的内置 Agent profile。"""

from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.model_settings import ModelSettings
from app.core.tool.schemas.tool_names import (
    TOOL_AGENT_TEAM,
    TOOL_APPLY_PATCH,
    TOOL_CHILD_AGENT_SEND,
    TOOL_CHILD_AGENT_STATUS,
    TOOL_DELEGATE_TASK,
    TOOL_DELETE_FILE,
    TOOL_EXECUTE_TERMINAL,
    TOOL_FIND_FILES,
    TOOL_LIST_DIRECTORY,
    TOOL_MCP,
    TOOL_MOVE_FILE,
    TOOL_PROPOSE_AGENT_CONFIGURATION,
    TOOL_PROPOSE_AGENT_TEAM_CONFIGURATION,
    TOOL_READ_FILE,
    TOOL_REPLACE,
    TOOL_SEARCH_CONTENT,
    TOOL_SKILL,
    TOOL_TERMINAL_CLOSE,
    TOOL_TERMINAL_LIST,
    TOOL_TERMINAL_READ,
    TOOL_TERMINAL_START,
    TOOL_TERMINAL_WRITE,
    TOOL_WAIT_MESSAGES,
    TOOL_WEB_EXTRACT,
    TOOL_WEB_SEARCH,
    TOOL_WRITE_FILE,
)

# 通用子 Agent 的系统预设正文（唯一事实源，装配时直接写入 profile）。
#
# 只承载该 profile 专属的执行协议，避免与 SystemPromptBuilder 其它层重复或冲突：身份与角色、
# 操作系统、工作区根目录与写入边界、可用工具集合、用户语言都在 ``<runtime_context>`` 层声明；
# 全局指令与 workspace 指令各自成层注入。故此处不复述这些事实，只声明「如何完成被委派的任务」。
#
# 协议的重心是「守住任务边界」：子 Agent 只看到父 Agent 给的那段任务文本，看不到用户原话与
# 主 Agent 的上文，一旦自行扩大范围（顺手重构、改配置、动任务没点名的文件），改动就发生在
# 无人审阅的上下文里，且用户工作区已被污染、无法追溯意图，因此「按指令做、不越界」优先于
# 「看起来更好」。
#
# 正文**不得**出现工具可用性（「你没有某工具」「你必须永不委派」之类）：同一 profile 在不同
# 场景下的工具面并不相同——作为 ``delegate_task`` 子 Agent 委派工具被剥掉，作为 Agent Team
# 节点则保留——写死任一侧都必然在另一侧变成假话，且工具事实本就由 ``<tool_layer>`` /
# ``<runtime_context>`` 声明。要约束行为就写成行为要求（「不得改动工作区」「不要绕过缺口」），
# 不要写成能力断言。
_GENERAL_CHILD_AGENT_SYSTEM_PROMPT = """\
You handle exactly one focused task delegated by the parent agent.

- The delegated instruction is your entire mandate: follow it as written. Anything the instruction
  does not mention stays out of scope, even when you believe changing it would be an improvement.
- Do not widen the scope: no extra refactors, no unrelated fixes, no renames, no dependency or
  configuration changes, and no touching files the task did not name. If you notice something else
  worth fixing, report it instead of fixing it.
- Make the smallest change that satisfies the instruction, and keep the existing style and
  conventions of the code you touch.
- Treat the global and workspace instructions in your context as binding. If they contradict the
  delegated task, say so instead of silently choosing a side.
- If the task is ambiguous, blocked, or needs a decision only the parent can make, stop and
  report what is missing instead of guessing.
- Verify your own result and say how you verified it. End with a concise report: the result, the
  files or evidence involved, and anything left unfinished.
"""

# 只读子 Agent 的系统预设正文（唯一事实源，装配时直接写入 profile）。
#
# 与通用子 Agent 同构：身份与角色、操作系统、工作区边界、可用工具集合、用户语言都由
# ``<runtime_context>`` / ``<tool_layer>`` 层声明，此处不重复。本常量只补该 profile 专属的
# 「只做调查取证、不动工作区」行为协议；同样**不写工具可用性**（同一 profile 在委派与 Team
# 节点两种场景下工具面不同），因此写的是「不得以任何方式改动工作区」这类行为要求，而不是
# 「你没有写类工具」这类能力断言——后者既可能在 Team 场景下失真，也会诱导模型去终端里绕。
_ONLY_READ_CHILD_AGENT_SYSTEM_PROMPT = """\
You handle exactly one read-only investigation task delegated by the parent agent.

- Investigate and report; never modify the workspace. Do not create, edit, delete, or move files,
  and do not change the workspace by any other means: if the task can only be completed by changing
  files, stop and report that it needs an agent allowed to change them.
- If you run commands, keep them non-mutating: read, list, search, or run existing code and tests.
  Do not issue commands whose purpose is to change the workspace (writing, deleting, installing,
  downloading); incidental caches produced by running existing code are acceptable.
- Work only on the delegated task: do not widen its scope or touch anything it did not ask about.
- Treat the global and workspace instructions in your context as binding.
- If the task is ambiguous, blocked, or needs a decision only the parent can make, stop and report
  what is missing instead of guessing.
- Report with evidence: back every claim with the file path and line numbers (or a short quote),
  say plainly what you could not verify, and end with a concise summary of findings.
"""


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
            TOOL_WRITE_FILE,
            TOOL_REPLACE,
            TOOL_APPLY_PATCH,
            TOOL_DELETE_FILE,
            TOOL_MOVE_FILE,
            TOOL_SEARCH_CONTENT,
            TOOL_FIND_FILES,
            TOOL_LIST_DIRECTORY,
            TOOL_EXECUTE_TERMINAL,
            TOOL_TERMINAL_LIST,
            TOOL_TERMINAL_START,
            TOOL_TERMINAL_READ,
            TOOL_TERMINAL_WRITE,
            TOOL_TERMINAL_CLOSE,
            TOOL_WEB_SEARCH,
            TOOL_WEB_EXTRACT,
            TOOL_DELEGATE_TASK,
            TOOL_CHILD_AGENT_SEND,
            TOOL_CHILD_AGENT_STATUS,
            TOOL_WAIT_MESSAGES,
            TOOL_PROPOSE_AGENT_TEAM_CONFIGURATION,
            TOOL_PROPOSE_AGENT_CONFIGURATION,
            TOOL_AGENT_TEAM,
            TOOL_MCP,
            TOOL_SKILL,
        ],
        agent_type=AgentProfileType.MAIN,
        system_prompt=system_prompt,
        max_steps=max_steps,
        model_settings=ModelSettings(stream=True, reasoning_effort="high"),
    )


def general_child_agent() -> AgentProfile:
    """构建由代码维护的通用委派子 Agent（进程启动时注册的默认 CHILD）。

    该 profile 由代码维护、不从 JSON 加载（可被同名 JSON 覆盖）。工具面覆盖读写文件、搜索、
    终端与网页，并**声明**了委派 / 父子通信工具：是否真正可用由执行方过滤决定——作为
    ``delegate_task`` 子 Agent 运行时它们被 ``CHILD_DISALLOWED_TOOLS`` 剥掉（子 Agent 不得再
    委派），作为 Agent Team 节点运行时则保留（节点需要把子工作再拆出去）。可交互终端不在
    工具面内。``description`` 面向主 Agent 说明何时该选它，``system_prompt`` 面向子 Agent
    强调「按指令做、不越界」。

    参数:
        无。

    返回:
        通用 CHILD profile，其 ``system_prompt`` 取自本模块的代码内常量
        ``_GENERAL_CHILD_AGENT_SYSTEM_PROMPT``。

    异常:
        无。

    副作用:
        无文件系统访问（提示词正文写死在代码中，不读取任何 Markdown 资源）。
    """

    return AgentProfile(
        agent_id="general-assistant",
        role="通用Agent",
        # 面向主 Agent 的选择说明：它出现在系统提示词的子 Agent 目录里，是主 Agent 决定「委派给
        # 谁」的依据。此处**只写什么时候该选它**，不写能力断言（「能读写」「不会再委派」之类）：
        # 同一 profile 在不同场景下工具面并不相同（``delegate_task`` 子 Agent vs Agent Team
        # 节点），且这份目录本身也会被 Team 节点读到，写死任一侧都会在另一侧变成假话。
        description=(
            "General-purpose child agent for a bounded task that does not fit a more specialized "
            "agent. Choose it for focused implementation, investigation, or analysis work whose "
            "scope fits one instruction, and spell out what to change and what to leave alone."
        ),
        # 可用工具：读写文件 + 搜索 + 终端 + 网页 + 委派/父子通信（后者由执行方按场景过滤）。
        allowed_tools=[
            TOOL_READ_FILE,
            TOOL_WRITE_FILE,
            TOOL_REPLACE,
            TOOL_APPLY_PATCH,
            TOOL_DELETE_FILE,
            TOOL_MOVE_FILE,
            TOOL_SEARCH_CONTENT,
            TOOL_FIND_FILES,
            TOOL_LIST_DIRECTORY,
            TOOL_EXECUTE_TERMINAL,
            TOOL_WEB_SEARCH,
            TOOL_WEB_EXTRACT,
            TOOL_DELEGATE_TASK,
            TOOL_CHILD_AGENT_SEND,
            TOOL_CHILD_AGENT_STATUS,
            TOOL_WAIT_MESSAGES,
        ],
        agent_type=AgentProfileType.CHILD,
        system_prompt=_GENERAL_CHILD_AGENT_SYSTEM_PROMPT,
        max_steps=300,
        model_settings=ModelSettings(stream=True, reasoning_effort="high"),
    )


def only_read_child_agent() -> AgentProfile:
    """构建由代码维护的「只读调查」委派子 Agent。

    工具面刻意只保留读取类能力（读文件、搜内容、找文件、列目录）加网页读取与终端，**不给**
    写文件 / 补丁 / 删除 / 移动，也不给可交互终端；委派 / 父子通信工具已声明，是否可用由执行
    方按场景过滤（与通用子 Agent 同一口径）。``description`` 面向主 Agent 说明何时该选它，
    ``system_prompt`` 说明它必须只取证、不动工作区。

    注意：终端仍在工具集内，它是「只读意图」的唯一破口（可执行任意命令），因此提示词以行为
    要求（不得以任何方式改动工作区）覆盖它；如需强约束应在工具面收紧而不是只靠提示词。

    参数:
        无。

    返回:
        只读 CHILD profile，其 ``system_prompt`` 取自本模块的代码内常量
        ``_ONLY_READ_CHILD_AGENT_SYSTEM_PROMPT``。

    异常:
        无。

    副作用:
        无文件系统访问（提示词正文写死在代码中，不读取任何 Markdown 资源）。
    """

    return AgentProfile(
        agent_id="only_read_child_agent",
        role="只读Agent",
        # 面向主 Agent 的选择说明：同样只写「什么时候该选它」，不写能力断言——理由与通用子
        # Agent 相同（同一 profile 在委派与 Team 节点两种场景下工具面不同，且这份目录会被
        # Team 节点读到）；工具面事实由 ``allowed_tools`` 与该 profile 的 docstring 承载。
        description=(
            "Read-only investigation child agent. Choose it when the delegated task is to inspect "
            "the workspace and report findings rather than to change it: reading and searching "
            "code, reconnaissance, fact gathering, or verifying a claim. Do not give it "
            "implementation or repair work."
        ),
        # 只读工具面：无写 / 补丁 / 删除 / 移动，无可交互终端；委派工具由执行方按场景过滤。
        allowed_tools=[
            TOOL_READ_FILE,
            TOOL_SEARCH_CONTENT,
            TOOL_FIND_FILES,
            TOOL_LIST_DIRECTORY,
            TOOL_EXECUTE_TERMINAL,
            TOOL_WEB_SEARCH,
            TOOL_WEB_EXTRACT,
            TOOL_DELEGATE_TASK,
            TOOL_CHILD_AGENT_SEND,
            TOOL_CHILD_AGENT_STATUS,
            TOOL_WAIT_MESSAGES,
        ],
        agent_type=AgentProfileType.CHILD,
        system_prompt=_ONLY_READ_CHILD_AGENT_SYSTEM_PROMPT,
        max_steps=300,
        model_settings=ModelSettings(stream=True, reasoning_effort="high"),
    )
