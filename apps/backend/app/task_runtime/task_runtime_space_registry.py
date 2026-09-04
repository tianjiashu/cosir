"""Backend 进程内已物化的 Task runtime space 注册表。

负责在进程生命周期内稳定持有每个 task_id 对应的 ``TaskRuntimeSpace`` 单例，
并提供整体清理入口。单个 space 的资源构成见 ``app.task_runtime.task_runtime_space``，
其持有的执行锁见 ``app.task_runtime.task_runtime_lock``。
"""

from __future__ import annotations

import threading

from app.task_runtime.task_runtime_space import TaskRuntimeSpace


class TaskRuntimeSpaceRegistry:
    """管理 backend 进程内已经物化的 Task runtime space。"""

    def __init__(self) -> None:
        """创建可供 backend 多线程访问的空 registry。"""

        self._spaces: dict[int, TaskRuntimeSpace] = {}
        self._guard = threading.Lock()

    def get_or_create(self, task_id: int) -> TaskRuntimeSpace:
        """返回 ``task_id`` 稳定对应的 runtime space。

        参数:
            task_id: 任务标识（整数 id）。

        返回:
            已存在或新创建的 ``TaskRuntimeSpace``；同一 ``task_id`` 始终返回同一实例。

        异常:
            无。

        副作用:
            首次为该 ``task_id`` 请求时构造并登记 ``TaskRuntimeSpace``；后续请求
            直接返回缓存实例。登记动作在 ``_guard`` 保护下进行，线程安全。
        """

        with self._guard:
            space = self._spaces.get(task_id)
            if space is None:
                space = TaskRuntimeSpace(task_id)
                self._spaces[task_id] = space
            return space

    def close(self) -> None:
        """所有执行停止后清理进程内的运行时投影。"""

        with self._guard:
            self._spaces.clear()


#: 进程级单例：多 space 的生命周期在 backend 进程内稳定持有一份。
task_runtime_spaces: TaskRuntimeSpaceRegistry = TaskRuntimeSpaceRegistry()
