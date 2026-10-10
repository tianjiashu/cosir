"""workspace 作用域键的共享定义与归一化（leaf 层纯字符串工具）。

单一职责：定义系统作用域哨兵，并把 workspace 根路径归一化为进程内稳定的作用域键。

为什么集中：Agent profile registry、Agent Team 配置 registry 与系统提示词/目录变更通知匹配
三处都要回答「这两个路径是不是同一个 workspace」。若各自实现，同一 workspace 会因大小写、
相对路径或符号链接写法产生不同键，表现为「配置明明在却匹配不到」的静默失效——这类缺陷不会
报错，只在行为上不成立，因此归一化口径必须唯一。

不负责：路径存在性校验、目录创建、判断某个 workspace 是否已在数据库中登记。
"""

from __future__ import annotations

import os
from pathlib import Path

SYSTEM_SCOPE: str = "system"
"""系统作用域哨兵：表示「不属于任何 workspace」的内置配置作用域。"""


def normalize_scope_path(workspace: str | Path) -> str:
    """把 workspace 路径归一化为作用域键。

    归一化在 ``abspath`` + ``normpath`` 基础上再按平台 ``normcase``，使 Windows 下大小写
    不同的同一路径归一到同一键；刻意不做 ``resolve``（不解析符号链接与挂载点），避免同一
    workspace 因「传入的是原路径还是解析后路径」而在不同调用点产生两个键。

    参数:
        workspace: workspace 根路径。

    返回:
        归一化后的作用域键字符串。

    异常:
        TypeError: 传入 ``None`` 等非路径对象时由 ``os.path.normpath`` 抛出。刻意不把
            ``None`` 转成字符串：那会把「没有 workspace」伪装成一个永不匹配的路径，让调用方
            的疏漏变成静默不匹配。

    副作用:
        无（纯字符串运算，不访问文件系统）。
    """

    return os.path.normcase(os.path.abspath(os.path.normpath(workspace)))
