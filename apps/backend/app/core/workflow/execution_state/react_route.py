"""业务循环的下一步枚举，与 Run 生命周期无关。"""

from enum import Enum


class ReactRoute(str, Enum):
    """由循环显式派发的有限步骤；END 表示本次驱动返回。"""

    PREPARE_INPUT = "prepare_input"
    MODEL = "model"
    TOOLS = "tools"
    OBSERVE = "observe"
    STRUCTURED_OUTPUT = "structured_output"
    WAIT_USER = "wait_user"
    END = "end"
