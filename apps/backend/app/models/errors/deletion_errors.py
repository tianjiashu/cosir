"""Task/workspace/run 删除领域异常。"""


class DeletionBusyError(RuntimeError):
    """表示目标资源正在执行不可并发的运行时操作。"""

    def __init__(self, resource_type: str, resource_id: int) -> None:
        """构造可供 API 映射的忙碌异常。

        参数:
            resource_type: 资源类型，取 ``task`` 或 ``workspace``。
            resource_id: 资源标识。

        返回:
            无。

        异常:
            ValueError: ``resource_type`` 不在支持范围内。
        """

        if resource_type not in {"task", "workspace"}:
            raise ValueError(f"unsupported deletion resource type: {resource_type}")
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.code = f"{resource_type.upper()}_BUSY"
        self.message = f"{resource_type} {resource_id} has an active operation"
        super().__init__(self.message)


class RunDeletionConflictError(RuntimeError):
    """删除 run 因目标 task 当前状态不允许而被拒绝。

    与 :class:`DeletionBusyError`（拿不到并发闸门）不同，本异常表达的是领域状态冲突：
    task 内存在 ``pending`` / ``running`` 的 active run 时删除任何 run 都会被拒绝，
    避免删除正在执行的 run 破坏执行器、租约与工具子进程。
    """

    def __init__(self, code: str, message: str) -> None:
        """构造可供 API 映射的 run 删除冲突异常。

        参数:
            code: 稳定的内部错误码（如 ``TASK_HAS_ACTIVE_RUN``）。
            message: 供诊断的可读信息，不包含密钥或用户输入。

        返回:
            无。

        副作用:
            无（仅承载 ``code`` / ``message``）。
        """

        super().__init__(message)
        self.code = code
        self.message = message
