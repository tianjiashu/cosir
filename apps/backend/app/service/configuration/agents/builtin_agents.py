"""系统内置子 Agent：出厂配置，以及配置文件缺失时的落盘。

内置子 Agent 的事实源是系统 ``.cosir/agents/<id>.json``，与用户新建的子 Agent 同一种文件、同一条装载
链路（``AgentProfileScopeSource`` → 注册表）。本模块只保存出厂默认值：启动装配时文件缺失才写一次，
之后一律以文件为准，用户在配置页的修改重启后照样生效。

「哪些 id 是内置」只由这里的清单决定，不写进可编辑的文件（否则手改文件就能去掉防删标记）；它只决定
两件事：配置中心拒绝删除、界面显示「内置」。注册表不知道内置这个概念。

文件被改坏（JSON 无效、字段不合规）时与用户文件同一策略：装载时跳过并记错误日志，不报错、不影响
应用、不进内存；落盘不覆盖已存在的文件，以免抹掉用户内容。想回到出厂值，删掉文件后重启即会重新生成。

出厂默认值放在代码里而不是随包分发的 JSON：后端由 PyInstaller 打包、只收集 Python 子模块，数据文件
要另加打包与路径解析；放在代码里还能保留提示词旁的设计理由注释。出厂值更新不会合并进已落盘的文件。
"""

from __future__ import annotations

from pathlib import Path

from app.config.logging.logger import log
from app.core.agents.model_settings import ModelSettings
from app.core.tool.schemas.tool_names import (
    TOOL_APPLY_PATCH,
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
    TOOL_READ_FILE,
    TOOL_READ_IMAGE,
    TOOL_SEARCH,
    TOOL_SEARCH_CONTENT,
    TOOL_SESSION_SEND,
    TOOL_SESSION_LIST,
    TOOL_SESSION_READ,
    TOOL_SESSION_STOP,
    TOOL_TERMINAL_LIST,
    TOOL_TERMINAL_READ,
    TOOL_TERMINAL_STOP,
    TOOL_WAIT_MESSAGES,
    TOOL_WEB_FETCH,
    TOOL_WEB_SEARCH,
    TOOL_WRITE_FILE,
)
from app.service.configuration import file_store
from app.service.configuration.agents.configuration import AgentConfiguration

# 通用子 Agent 的系统预设正文。
#
# 只承载该 profile 专属的执行协议，避免与 SystemPromptBuilder 其它层重复或冲突：身份与角色、
# 操作系统、工作区根目录与写入边界、可用工具集合、用户语言都在 ``<runtime_context>`` 层声明；
# 全局指令与 workspace 指令各自成层注入。故此处不复述这些事实，只声明「如何完成被委派的会话」。
#
# 协议的重心是「守住会话边界」：子 Agent 只看到父 Agent 给的那段会话文本，看不到用户原话与
# 主 Agent 的上文，一旦自行扩大范围（顺手重构、改配置、动会话没点名的文件），改动就发生在
# 无人审阅的上下文里，且用户工作区已被污染、无法追溯意图，因此「按指令做、不越界」优先于
# 「看起来更好」。
#
# 正文**不得**出现工具可用性（「你没有某工具」「你必须永不委派」之类）：同一 profile 在不同场景下的
# 工具面并不相同——能不能再委派取决于派生深度，委派子 Agent 拿到的工具还要按
# Session 类型规则过滤（``models.session_kind``）——写死任一侧都必然在另一侧变成假话，且工具事实
# 本就由 ``<tool_layer>`` / ``<runtime_context>`` 声明。要约束行为就写成行为要求
# （「不得改动工作区」「不要绕过缺口」），不要写成能力断言。
# 交差写法：三个内置子 Agent 共用。报告只经最后一条消息交给父 Agent（由编排作为 Agent 消息汇报），
# 另写的报告文件父 Agent 不会读；贴大段读过的代码只会把父 Agent 的上下文撑大。
_REPORT_RULES = """\
- Your final message is your report: do not write report, summary, or findings files. Include code
  snippets only when the exact text is the evidence (a bug, a signature the task asked for); do not
  recap code you merely read.
"""

_GENERAL_CHILD_AGENT_SYSTEM_PROMPT = f"""\
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
- If the task is ambiguous, blocked, or needs a decision only the parent can make, do not guess:
  ask the agent that gave you the task and wait for its answer when you can reach it, otherwise
  stop and report what is missing.
- You are already the agent for this task: do the work yourself. Do not hand the whole task on to
  another child agent; delegate only an independent part of it, if anything.
- Verify your own result and say how you verified it. End with a concise report: the result, the
  files or evidence involved, and anything left unfinished.
{_REPORT_RULES}"""

# 只读规则：Explore 与 Plan 共用。
#
# 与通用子 Agent 同构：身份与角色、操作系统、工作区边界、可用工具集合、用户语言都由
# ``<runtime_context>`` / ``<tool_layer>`` 层声明，此处不重复；同样**不写工具可用性**（工具面随派生深度
# 与 Session 类型规则变化），因此写的是「不得以任何方式改动工作区」这类行为要求，而不是「你没有写类
# 工具」这类能力断言——后者可能失真，也会诱导模型去终端里绕。
#
# 终端仍在它们的工具集内，是「只读意图」的唯一破口（可执行任意命令），因此提示词逐条列出经终端写入
# 的常见写法（重定向、heredoc、临时文件、移动复制、安装），比「不要改动工作区」这类抽象要求更难被绕过；
# 不列具体命令名，因为 shell 随平台不同。如需强约束应在工具面收紧而不是只靠提示词。
_READ_ONLY_RULES = """\
- Never modify the workspace. Do not create, edit, delete, or move files, and do not change the
  workspace by any other means: if the task can only be completed by changing files, stop and
  report that it needs an agent allowed to change them.
- Use the terminal only for commands that read: listing, printing, and searching files, inspecting
  git history and status, and running existing code or tests. Never use it to write: no output
  redirection (>, >>) or heredocs into files, no temporary or scratch files anywhere, no creating,
  moving, copying, or deleting files or directories, no git commands that change the repository
  (add, commit, checkout, reset, stash), and no installing or downloading. Incidental caches
  produced by running existing code are acceptable.
- Work only on the delegated task: do not widen its scope or touch anything it did not ask about.
- Treat the global and workspace instructions in your context as binding.
- If the task is ambiguous, blocked, or needs a decision only the parent can make, do not guess:
  ask the agent that gave you the task and wait for its answer when you can reach it, otherwise
  stop and report what is missing.
"""

# Explore 的系统预设正文：只读规则加上它的描述对主 Agent 许下的两条行为——按调用方指定的广度搜，
# 读片段而不是整个文件。描述与正文必须一致，改一边要同步另一边。
_EXPLORE_SYSTEM_PROMPT = f"""\
You handle exactly one read-only search task delegated by the parent agent: find where things are
and report what you found.

{_READ_ONLY_RULES}\
- Search at the breadth the task asks for: "medium" means the likely locations; "very thorough"
  means every plausible location, naming convention, and spelling variant. If no breadth is given,
  use "medium".
- Search first, then read only the excerpts around the matches; read a whole file only when the
  task needs it. You locate code: do not review or audit it unless the task asks for that.
- Report with evidence: back every claim with the file path and line numbers (or a short quote),
  say plainly what you could not find or verify, and end with a concise summary of findings.
{_REPORT_RULES}"""

# Plan 的系统预设正文：只读规则加上「先查后定、交出可验证的方案」。方案给父 Agent 照着做，所以要求
# 写到文件与步骤，并附上现有代码的证据；结尾固定一节关键文件，父 Agent 接手时从这里开始读。不实现，
# 实现交给父 Agent 或可写的子 Agent。
_PLAN_SYSTEM_PROMPT = f"""\
You design an implementation plan for one task delegated by the parent agent. You plan; you do not
implement.

{_READ_ONLY_RULES}\
- Investigate before planning: read the code the change will touch, its callers and consumers, and
  existing code that already does something similar. Prefer reusing what exists over adding new
  code.
- Deliver the plan: the recommended approach and why; the alternatives you rejected and why; the
  critical files to change and what changes in each; the steps in an order where each one can be
  verified; the risks, open questions, and how to verify the finished result.
- Back every claim about existing code with the file path and line numbers, and keep the plan as
  short as the change allows.
{_REPORT_RULES}\
- End the plan with a section titled "Critical files" listing the 3-5 workspace files most critical
  to implementing it, one path per line.
"""

# 只读工具面：无写 / 补丁 / 删除 / 移动，无可交互终端。
_READ_ONLY_TOOLS = (
    TOOL_READ_FILE,
    TOOL_READ_IMAGE,
    TOOL_SEARCH_CONTENT,
    TOOL_FIND_FILES,
    TOOL_EXECUTE_TERMINAL,
    TOOL_TERMINAL_LIST,
    TOOL_TERMINAL_READ,
    TOOL_TERMINAL_STOP,
    TOOL_WEB_SEARCH,
    TOOL_WEB_FETCH,
    TOOL_DELEGATE_TASK,
    TOOL_SESSION_SEND,
    TOOL_SESSION_LIST,
    TOOL_SESSION_READ,
    TOOL_SESSION_STOP,
    TOOL_WAIT_MESSAGES,
    # 外部工具（MCP）的入口：子 Agent 也能用 MCP。
    TOOL_SEARCH,
)

_DEFAULT_MODEL_SETTINGS = ModelSettings(stream=True, reasoning_effort="high").to_dict()

# 出厂配置。``description`` 面向主 Agent，是系统提示词子 Agent 目录里的唯一内容（目录不列工具），是
# 「委派给谁」的依据：写清定位、什么时候选它、它不做什么，以及委派时要交代什么。能力边界按角色写
# （「只读」「不实现」），由该 Agent 的系统提示词兑现；不写随场景变化的工具可用性（「不会再委派」之类）
# ——同一 profile 的工具面随派生深度与 Session 类型规则变化。可交互终端不给子 Agent。
BUILTIN_CHILD_AGENTS: tuple[AgentConfiguration, ...] = (
    AgentConfiguration(
        agent_id="general-purpose",
        role="通用Agent",
        description=(
            "General-purpose agent for researching complex questions, searching for code, and "
            "executing multi-step tasks. When you are searching for a keyword or file and are not "
            "confident that you will find the right match in the first few tries use this agent to "
            "perform the search for you."
        ),
        system_prompt=_GENERAL_CHILD_AGENT_SYSTEM_PROMPT,
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
            TOOL_SESSION_SEND,
            TOOL_SESSION_LIST,
            TOOL_SESSION_READ,
            TOOL_SESSION_STOP,
            TOOL_WAIT_MESSAGES,
            TOOL_SEARCH,
        ],
        max_steps=300,
        model_settings=dict(_DEFAULT_MODEL_SETTINGS),
    ),
    AgentConfiguration(
        agent_id="Explore",
        role="探索Agent",
        description=(
            "Read-only search agent for broad fan-out searches — when answering means sweeping "
            "many files, directories, or naming conventions and you only need the conclusion, not "
            "the file dumps. It reads excerpts rather than whole files, so it locates code; it "
            'doesn\'t review or audit it. Specify search breadth: "medium" for moderate '
            'exploration, "very thorough" for multiple locations and naming conventions.'
        ),
        system_prompt=_EXPLORE_SYSTEM_PROMPT,
        allowed_tools=list(_READ_ONLY_TOOLS),
        max_steps=300,
        model_settings=dict(_DEFAULT_MODEL_SETTINGS),
    ),
    AgentConfiguration(
        agent_id="Plan",
        role="规划Agent",
        description=(
            "Software architect agent for designing implementation plans. Use this when you need "
            "to plan the implementation strategy for a task. Returns step-by-step plans, "
            "identifies critical files, and considers architectural trade-offs."
        ),
        system_prompt=_PLAN_SYSTEM_PROMPT,
        allowed_tools=list(_READ_ONLY_TOOLS),
        max_steps=300,
        model_settings=dict(_DEFAULT_MODEL_SETTINGS),
    ),
)

BUILTIN_CHILD_AGENT_IDS: frozenset[str] = frozenset(agent.agent_id for agent in BUILTIN_CHILD_AGENTS)


def seed_builtin_agent_files(directory: Path) -> None:
    """把缺失的内置子 Agent 配置文件写成出厂值；已存在的文件（哪怕被改坏）一律不动。

    启动装配时、系统作用域首次装载之前调用，``directory`` 须与注册表装载用的是同一个目录映射
    （``registry.source.directory(SYSTEM)``），否则写下的文件读取方看不到。

    写入失败只记错误日志、不抛出：内置子 Agent 缺席不该挡住应用启动，下次启动会再试。
    """

    for agent in BUILTIN_CHILD_AGENTS:
        path = directory / f"{agent.agent_id}.json"
        if path.exists():
            continue
        try:
            file_store.write_json(path, agent.to_json_document(), root=directory)
        except OSError as exc:
            log.error(
                "builtin_agent_seed_failed",
                extra={
                    "msg": "内置子 Agent 配置文件生成失败，本次启动没有这个 Agent",
                    "data": {"agent_id": agent.agent_id, "path": str(path), "error_type": type(exc).__name__},
                },
            )
            continue
        log.info(
            "builtin_agent_seeded",
            extra={
                "msg": "内置子 Agent 配置文件缺失，已写入出厂值",
                "data": {"agent_id": agent.agent_id, "path": str(path)},
            },
        )
