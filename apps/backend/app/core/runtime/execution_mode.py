"""显式执行入口：fresh 开始新 Run，resume 从模型边界继续，resume_with_input 消费持久化决定。"""

from typing import Literal

ExecutionMode = Literal["fresh", "resume", "resume_with_input"]
"""``fresh`` 创建新的工作流输入；``resume`` 从模型边界继续。"""
