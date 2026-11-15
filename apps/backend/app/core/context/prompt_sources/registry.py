"""系统提示词配置来源的登记表：全部来源与它们的投影顺序。

新增一类「Agent 能看到的配置」只需实现 ``PromptSource`` 并登记在这里；提示词渲染与基准记录都遍历它。
"""

from __future__ import annotations

from app.core.context.prompt_sources.source import PromptSource
from app.core.context.prompt_sources.sources.agent_preset import AgentPresetSource
from app.core.context.prompt_sources.sources.child_agents import ChildAgentsSource
from app.core.context.prompt_sources.sources.global_instructions import GlobalInstructionsSource
from app.core.context.prompt_sources.sources.mcp_servers import MCPServersSource
from app.core.context.prompt_sources.sources.reply_language import ReplyLanguageSource
from app.core.context.prompt_sources.sources.skills import SkillsSource
from app.core.context.prompt_sources.sources.workspace_instructions import (
    WorkspaceInstructionsSource,
)

AGENT_PRESET = AgentPresetSource()
CHILD_AGENTS = ChildAgentsSource()
MCP_SERVERS = MCPServersSource()
SKILLS = SkillsSource()
GLOBAL_INSTRUCTIONS = GlobalInstructionsSource()
WORKSPACE_INSTRUCTIONS = WorkspaceInstructionsSource()
REPLY_LANGUAGE = ReplyLanguageSource()

PROMPT_SOURCES: tuple[PromptSource, ...] = (
    AGENT_PRESET,
    CHILD_AGENTS,
    MCP_SERVERS,
    SKILLS,
    GLOBAL_INSTRUCTIONS,
    WORKSPACE_INSTRUCTIONS,
    REPLY_LANGUAGE,
)
