"""推理强度的取值集合：用户为一个 Run 选定、Agent 偏好声明与模型请求共用这一份。"""

from typing import Literal

ReasoningEffort = Literal["low", "high", "max"]
