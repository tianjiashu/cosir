"""系统提示词增量来源枚举；不依赖具体模型 SDK 或消息实现。"""

from __future__ import annotations

from enum import Enum


class SystemPromptDeltaSource(str, Enum):
    """系统提示词变更的来源层。"""

    GLOBAL_INSTRUCTIONS = "global_instructions"
    MAIN_AGENT_PROMPT = "main_agent_prompt"
    WORKSPACE_INSTRUCTIONS = "workspace_instructions"
