"""进程所在宿主操作系统的判定。

``is_windows`` 是「宿主是不是 Windows」的唯一判定口径：参数模型选型、shell 定位与命令启动都
走它，避免各处用 ``os.name`` / ``platform.system()`` 各写一份而在大小写等细节上分叉。
``host_platform_name`` 只供向模型或用户暴露「运行环境」语义的边界使用（如系统提示词的运行期
事实层）。
"""
from __future__ import annotations

import platform
from functools import lru_cache


def is_windows(system: str | None = None) -> bool:
    """判断宿主是否 Windows（大小写与首尾空白容错）。

    ``system`` 取 ``platform.system()`` 的形态；显式传入时优先于宿主实况，供工具描述与测试
    固定平台。未识别的平台名一律按非 Windows（POSIX）处理。
    """
    name = platform.system() if system is None else system
    return name.strip().lower() == "windows"


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
