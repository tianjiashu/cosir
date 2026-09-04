"""Hook 事件枚举。

单一职责：承载「Hook 触发的时机」这一枚举一维事实。枚举值同时是
``HookRegistry`` 的索引键（按 ``event`` 匹配订阅），因此是 Hook 机制的
契约单一事实来源之一（另一单一事实来源是 ``HookContext`` / ``HookResult``）。

枚举值使用 Claude Code 风格命名以对齐用户已确认的事件分类：
``USER_PROMPT_SUBMIT`` / ``PRE_TOOL_USE`` / ``POST_TOOL_USE`` / ``SESSION_START`` /
``SESSION_END`` / ``STOP`` / ``PRE_COMPACT``。
"""

from __future__ import annotations

from enum import Enum


class HookEvent(Enum):
    """Hook 触发时机枚举。

    取值与 Claude Code 7 类 Hook 对齐，覆盖用户消息提交、工具前后置、
    会话起止、轮次结束、压缩前六个切面。每个值都是 ``HookRegistry`` 的
    索引键，订阅方按此枚举注册，触发方按此枚举 ``fire``。
    """

    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"
    STOP = "Stop"
    PRE_COMPACT = "PreCompact"


class HookDecision(Enum):
    """Hook 返回的决策枚举。

    ``ALLOW`` 放行（或改写参数后放行）；``DENY`` 硬拒绝（当前仅 ``PRE_TOOL_USE``
    消费，为硬拒绝直接终止执行）。无 ``ASK``——首版不支持需要等待
    用户回答的阻塞型交互（``Hook机制技术方案.md`` §5.1）。
    """

    ALLOW = "allow"
    DENY = "deny"
