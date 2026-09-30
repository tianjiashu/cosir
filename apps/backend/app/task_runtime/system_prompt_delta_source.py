"""系统提示词增量的来源层枚举。

单一职责：只定义 :class:`SystemPromptDeltaSource`，描述一条系统提示词增量来自哪一层配置。
``SystemPromptDelta`` 的值对象与 LangChain 消息映射见 ``system_prompt_delta.py``，本模块不依赖它，
避免值对象引入 LangChain 依赖时反向拖累这个纯枚举。
"""

from __future__ import annotations

from enum import Enum


class SystemPromptDeltaSource(str, Enum):
    """系统提示词变更的来源层。"""

    GLOBAL_INSTRUCTIONS = "global_instructions"
    MAIN_AGENT_PROMPT = "main_agent_prompt"
    WORKSPACE_INSTRUCTIONS = "workspace_instructions"
