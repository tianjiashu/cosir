"""core 对「Agent 自己在后台跑的终端命令」的端口：依赖倒置，core 不导入 ``app.tools``。

后台命令是终端工具的事（结论路由与撤销归 ``app.tools.tool_handler.terminal.command_results``），但 core
有三处需要知道它们：Task 是否还有没了结的下游工作（``derived_report.has_open_work``，汇报的「中间结果」）、``wait_messages`` 还能等什么、上游收回时撤销它们（``TaskRecall``）。core 只经这里问，
实现由 lifespan 装配时注入（测试由 conftest 注入），未注入就调用属于装配缺陷，直接报错，不静默降级。
"""

from __future__ import annotations

from typing import Protocol


class BackgroundCommands(Protocol):
    """core 用到的那几个问题；实现是终端工具层的命令结果登记表。"""

    def has_work(self, task_id: int) -> bool:
        """Task 还有没交付完的命令：登记着的条目，或已出队、正在写入队列的结论。"""
        ...

    def pending(self, task_id: int) -> list[str]:
        """Task 仍在后台运行的命令标签，按登记先后。"""
        ...

    def recall_task(self, task_id: int) -> dict[str, dict[str, object] | None]:
        """上游收回：撤销 Task 的全部命令并等它们限时收尾；返回被撤销的后台调用及卡片终态展示。"""
        ...


_commands: BackgroundCommands | None = None


def set_background_commands(commands: BackgroundCommands | None) -> None:
    """装配期注入实现；``None`` 解除（进程关闭后）。"""
    global _commands
    _commands = commands


def background_commands() -> BackgroundCommands:
    """取已注入的实现。

    异常:
        RuntimeError: 尚未装配（lifespan 之外直接调用 core 而没有注入）。
    """
    if _commands is None:
        raise RuntimeError("background commands are not assembled")
    return _commands
