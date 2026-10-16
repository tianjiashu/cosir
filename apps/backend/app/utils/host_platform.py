"""进程所在宿主操作系统的稳定可读名称。

仅供需要向模型或用户暴露「运行环境」语义的边界使用（如运行期通知前缀），
不承载系统兼容性分支逻辑；兼容性判定应走既有的路径/平台工具。
"""
from __future__ import annotations

import platform
from functools import lru_cache


@lru_cache(maxsize=1)
def host_platform_name() -> str:
    """返回面向模型/用户的可读操作系统名：macOS / Windows / Linux / <其它>。"""
    name = platform.system()
    if name == "Darwin":
        return "mac"
    if name == "Windows":
        return "Windows"
    if name == "Linux":
        return "Linux"
    return name or "Unknown"
