"""工作区相对路径的基础合法性原子校验。

供浏览（``workspace_paths``）、工具安全（``PathResolver``）与文件改写
（``file_operation_paths``）三处共享，避免同一段「NUL / 父目录分量」判定
被反复手写而产生口径漂移。各调用方仍按自身边界组合这些原子谓词——
例如浏览侧允许空串与 ``.`` 表示工作区根、macOS 下允许反斜杠作为文件名
字符，这类差异由调用方保留，本模块只承载跨场景无争议的最小共性。
"""

from __future__ import annotations


def has_nul(value: str) -> bool:
    """路径是否含 NUL 字节；含 NUL 的路径在系统调用层非法，任何路径口径都拒绝。"""
    return "\x00" in value


def has_parent_segment(value: str) -> bool:
    """路径是否含 ``..`` 父目录分量；允许 ``..`` 会让解析后的真实路径逃逸工作区根。"""
    return ".." in value.replace("\\", "/").split("/")


__all__ = ["has_nul", "has_parent_segment"]
