"""Task/workspace 删除领域异常。"""


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
