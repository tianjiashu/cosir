"""定义必须保留在代码中的内置 Agent profile。"""

from pathlib import Path

from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.model_settings import ModelSettings
from app.core.tools.schemas.tool_names import ALL_TOOL_NAMES
from app.utils.file_utils import read_text_file


def _load_system_prompt(filename: str) -> str:
    """读取内置 Agent 的系统提示词正文。

    参数:
        filename: 位于 `app/core/context/system_prompt` 下的文件名。

    返回:
        已读取的非空 UTF-8 系统提示词正文。

    异常:
        RuntimeError: 提示词资源缺失、不可读、编码无效或内容为空；启动装配应失败，避免
            Agent 使用空提示词运行。

    副作用:
        读取随应用分发的内置提示词文件。
    """

    path = Path(__file__).resolve().parent.parent / "context" / "system_prompt" / filename
    try:
        prompt = read_text_file(path)
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"内置 Agent 系统提示词无法读取: {path}") from exc
    if not prompt.strip():
        raise RuntimeError(f"内置 Agent 系统提示词为空: {path}")
    return prompt


def _all_tool_names() -> list[str]:
    """返回工具规范清单中的全部名称。"""

    return list(ALL_TOOL_NAMES)


def main_agent() -> AgentProfile:
    """构建负责理解用户目标、编排工作并汇总结果的主 Agent。"""

    return AgentProfile(
        agent_id="main_agent",
        role="main_agent",
        allowed_tools=_all_tool_names(),
        agent_type=AgentProfileType.MAIN,
        system_prompt=_load_system_prompt("main_agent.md"),
        max_steps=300,
        model_settings=ModelSettings(thinking=True, stream=True, reasoning_effort="high"),
    )


def general_child_agent() -> AgentProfile:
    """构建始终可用、由代码维护的通用委派子 Agent。

    该 profile 是唯一不从 JSON 加载的 CHILD Agent。它提供通用委派能力，具体工具仍受
    `CHILD_BANNED_TOOLS` 和运行期 workspace 边界约束。

    参数:
        无。

    返回:
        通用 CHILD profile。

    异常:
        RuntimeError: 通用子 Agent 的内置提示词资源无法读取或内容为空。

    副作用:
        读取通用子 Agent 随应用分发的提示词文件，并将正文放入 profile。
    """

    return AgentProfile(
        agent_id="general-assistant",
        role="general-assistant",
        description=(
            "General-purpose child agent for a focused task that does not fit a more specialized "
            "agent. Choose this for bounded implementation, investigation, or analysis work."
        ),
        allowed_tools=_all_tool_names(),
        agent_type=AgentProfileType.CHILD,
        system_prompt=_load_system_prompt("general_child_agent.md"),
        max_steps=300,
        model_settings=ModelSettings(thinking=True, stream=True, reasoning_effort="high")
    )
