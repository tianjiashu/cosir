"""terminal_list 工具的参数契约。"""

from pydantic import BaseModel, ConfigDict


class TerminalListArgs(BaseModel):
    """列出当前 Task 的终端会话；会话归属取自执行上下文，模型不能指定别的 Task。"""

    model_config = ConfigDict(strict=True, extra="forbid")
