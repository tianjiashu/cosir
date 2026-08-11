"""子 Agent 能力摘要投影（core 层纯函数）。

单一职责：把 ``AgentProfileRegistry`` 中的委派子 Agent（约定 ``agent_id`` 以
``delegate_`` 前缀）投影成一段面向父 Agent（模型）的可读能力摘要文本，供注入到
``delegate_task`` 工具描述中，使父 Agent 在调用时知悉可选子 Agent 及其能力边界。

本模块只依赖 ``core/agents`` 内部纯数据结构，不依赖 service / tools / api，
确保 core 层不被上层反向污染；摘要经由 ``config/configuration`` 单例中转，
最终由 ``tools/tool_system`` 读取注入（见分层约束）。

不重复定义 ``AgentProfileToolSummary``：该值对象已在 ``agent_profile.py`` 由 Task 1
落地（``to_tool_summary`` 返回它），本模块直接复用，避免同名类分裂与循环 import。
"""

from __future__ import annotations

from app.core.agents.agent_profile import AgentProfileToolSummary
from app.core.agents.agent_profile_registry import AgentProfileRegistry

# 子 Agent 标识前缀约定：仅这些 agent_id 以该前缀开头的 profile 才会被投影进摘要。
DELEGATE_AGENT_ID_PREFIX = "delegate_"

# 摘要中各能力区块的固定英文标签（面向模型的文本用英文，项目约定）。
_CAPABILITIES_LABEL = "capabilities"
_RECOMMENDED_USE_CASES_LABEL = "recommended use cases"
_CONSTRAINTS_LABEL = "constraints"


def project_child_agent_summary(registry: AgentProfileRegistry) -> str:
    """把注册表中全部委派子 Agent 投影为面向父 Agent 的能力摘要字符串。

    遍历 ``registry``（使用其公开 ``list`` 接口，不触碰私有字段），仅筛选
    ``agent_id`` 以 ``delegate_`` 前缀的子 Agent，逐个调用 ``to_tool_summary()``
    投影，再拼成统一的英文标签摘要文本。摘要刻意**不含** ``workflow``/
    ``context_policy``/``prompt_ref`` 等运行时字段（由 ``to_tool_summary`` 保证）。
    父 Agent（developer/developer_pro）因不以该前缀开头，不会被纳入。

    参数:
        registry: 已播种完成的 agent profile 目录；其 ``list`` 接口返回全部已注册 profile。

    返回:
        形如 ``"Available child agents:\\n- delegate_reviewer (...): ...\\n..."`` 的
        摘要文本；若注册表中无任何委派子 Agent，返回空字符串。

    异常:
        无（纯函数，只读 registry，不抛预期异常）。

    副作用:
        无（不修改 registry，不写入任何外部状态）。
    """

    blocks: list[str] = []
    for profile in registry.list():
        if not profile.agent_id.startswith(DELEGATE_AGENT_ID_PREFIX):
            continue
        summary: AgentProfileToolSummary = profile.to_tool_summary()
        capabilities = "; ".join(summary.capabilities) if summary.capabilities else ""
        use_cases = (
            "; ".join(summary.recommended_use_cases)
            if summary.recommended_use_cases
            else ""
        )
        constraints = "; ".join(summary.constraints) if summary.constraints else ""
        block_lines = [
            f"- {summary.agent_id} ({summary.role}): {summary.description}",
            f"  {_CAPABILITIES_LABEL}: {capabilities}",
            f"  {_RECOMMENDED_USE_CASES_LABEL}: {use_cases}",
            f"  {_CONSTRAINTS_LABEL}: {constraints}",
            f"  {summary.tool_capability_summary}",
        ]
        blocks.append("\n".join(block_lines))

    if not blocks:
        return ""
    return "Available child agents:\n" + "\n".join(blocks)
