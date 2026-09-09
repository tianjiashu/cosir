"""Backend 进程内已物化的 Task runtime space 注册表。

负责在进程生命周期内稳定持有每个 task_id 对应的 ``TaskRuntimeSpace`` 单例，
并提供单个及整体清理入口。单个 space 的资源构成与 Task 操作锁见
``app.task_runtime.task_runtime_space``。
"""

from __future__ import annotations

import threading

from app.task_runtime.task_runtime_space import TaskRuntimeSpace


class TaskRuntimeSpaceRegistry:
    """管理 backend 进程内已经物化的 Task runtime space。"""

    def __init__(self) -> None:
        """创建可供 backend 多线程访问的空 registry。"""

        self._spaces: dict[int, TaskRuntimeSpace] = {}
        self._deleted_task_ids: set[int] = set()
        self._guard = threading.Lock()

    def get_or_create(self, task_id: int) -> TaskRuntimeSpace:
        """返回 ``task_id`` 稳定对应的 runtime space。

        参数:
            task_id: 任务标识（整数 id）。

        返回:
            已存在或新创建的 ``TaskRuntimeSpace``；同一 ``task_id`` 始终返回同一实例。

        异常:
            KeyError: 如果该 Task 已经完成删除并被标记为不可重新物化。

        副作用:
            首次为该 ``task_id`` 请求时构造并登记 ``TaskRuntimeSpace``；后续请求
            直接返回缓存实例。登记动作在 ``_guard`` 保护下进行，线程安全。
        """

        with self._guard:
            if task_id in self._deleted_task_ids:
                raise KeyError(task_id)
            space = self._spaces.get(task_id)
            if space is None:
                space = TaskRuntimeSpace(task_id)
                self._spaces[task_id] = space
            return space

    def get(self, task_id: int) -> TaskRuntimeSpace | None:
        """返回已经登记的 Task runtime space，不触发惰性创建。"""

        with self._guard:
            return self._spaces.get(task_id)

    def mark_deleted(self, task_id: int) -> None:
        """禁止已删除 Task 再次惰性创建 runtime space。"""

        with self._guard:
            self._deleted_task_ids.add(task_id)

    def unload(
        self,
        task_id: int,
        *,
        expected_space: TaskRuntimeSpace | None = None,
    ) -> None:
        """卸载一个已经停止使用的 Task runtime space。

        参数:
            task_id: 待卸载任务标识。
            expected_space: 可选的身份校验对象；传入时只卸载仍指向该实例的条目，
                防止旧删除流程误删后来重新创建的 runtime space。

        返回:
            无。

        异常:
            无。

        副作用:
            从进程内 registry 删除一个 Task runtime space；不会停止执行中的任务。
        """

        with self._guard:
            current = self._spaces.get(task_id)
            if current is None:
                return
            if expected_space is not None and current is not expected_space:
                return
            self._spaces.pop(task_id, None)

    def close(self) -> None:
        """所有执行停止后清理进程内的运行时投影。"""

        with self._guard:
            self._spaces.clear()
            self._deleted_task_ids.clear()


#: 进程级单例：多 space 的生命周期在 backend 进程内稳定持有一份。
task_runtime_spaces: TaskRuntimeSpaceRegistry = TaskRuntimeSpaceRegistry()
