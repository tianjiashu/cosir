"""Conversation Run 的执行模式。"""

from typing import Literal

ExecutionMode = Literal["fresh", "resume"]
"""``fresh`` 创建新的工作流输入；``resume`` 从既有 checkpoint 继续。"""
