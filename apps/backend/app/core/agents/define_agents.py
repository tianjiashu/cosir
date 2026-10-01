"""定义必须保留在代码中的内置 Agent profile。"""

from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.model_settings import ModelSettings
from app.core.tools.schemas.tool_names import *

# 通用子 Agent 的系统预设正文（唯一事实源，装配时直接写入 profile）。
#
# 只承载该 profile 专属的执行协议，避免与 SystemPromptBuilder 其它层重复或冲突：身份与角色、
# 操作系统、工作区根目录与写入边界、可用工具集合、用户语言都在 ``<runtime_context>`` 层声明；
# 全局指令与 workspace 指令各自成层注入。故此处不复述这些事实，只声明「如何完成被委派的任务」。
_GENERAL_CHILD_AGENT_SYSTEM_PROMPT = """\
You handle exactly one focused task delegated by the parent agent.

- Work only on the delegated task: do not widen its scope, refactor unrelated code, or change
  anything the task did not ask for.
- You have no sub-agents and must never delegate; do the work yourself.
- Treat the global and workspace instructions in your context as binding.
- If the task is ambiguous, blocked, or needs a decision only the parent can make, stop and
  report what is missing instead of guessing.
- End with a concise report: the result, the files or evidence involved, how you verified it, and
  anything left unfinished.
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
            TOOL_TERMINAL_START,
            TOOL_TERMINAL_READ,
            TOOL_TERMINAL_WRITE,
            TOOL_TERMINAL_SIGNAL,
            TOOL_TERMINAL_CLOSE,
            TOOL_WEB_SEARCH,
            TOOL_WEB_EXTRACT,
            TOOL_DELEGATE_TASK,
            TOOL_CHILD_AGENT_SEND,
            TOOL_CHILD_AGENT_STATUS,
            TOOL_CHILD_AGENT_WAIT,
        ],
        agent_type=AgentProfileType.MAIN,
        system_prompt=system_prompt,
        max_steps=max_steps,
        model_settings=ModelSettings(stream=True, reasoning_effort="high"),
    )


def general_child_agent() -> AgentProfile:
    """构建始终可用、由代码维护的通用委派子 Agent。

    该 profile 是唯一不从 JSON 加载的 CHILD Agent。它提供通用委派能力，具体工具仍受
    child Run 的 ``allows_tools`` 和运行期 workspace 边界约束。

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
        role="general-assistant",
        description=(
            "General-purpose child agent for a focused task that does not fit a more specialized "
            "agent. Choose this for bounded implementation, investigation, or analysis work."
        ),
        #不可以使用子Agent、可交互终端tool
        allowed_tools= [
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
        ],
        agent_type=AgentProfileType.CHILD,
        system_prompt=_GENERAL_CHILD_AGENT_SYSTEM_PROMPT,
        max_steps=300,
        model_settings=ModelSettings(stream=True, reasoning_effort="high")
    )
