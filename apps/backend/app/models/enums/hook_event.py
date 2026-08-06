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
