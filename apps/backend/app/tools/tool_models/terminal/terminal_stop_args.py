"""terminal_stop 工具的参数契约。"""

from pydantic import BaseModel, ConfigDict, Field


class TerminalStopArgs(BaseModel):
    """停掉自己在 execute_terminal 里起的命令；用户开的终端不在此列。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    terminal_id: str = Field(
        min_length=1,
        max_length=128,
        description="terminal_id of your command still running from execute_terminal.",
    )
