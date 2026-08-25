"""``file_change_updated`` 事件 payload 值对象。"""

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class FileChangeUpdatedPayload(RuntimeEventPayload):
    """某文件变更在运行中（turn 进行中）产生或更新，需前端增量展示。

    与 ``file_change_stable`` 不同：本事件在工具执行落库快照后立即广播，
    不持久化到 ``runtime_events`` 表，仅作 SSE 增量提示；数据源仍在
    ``file_snapshots`` 表（含 ``stable=0`` 的运行中态），前端可靠
    ``GET /tasks/{id}/changes`` 全量校准。

    为支持前端「工具返回立刻显示 diff」的零延迟渲染，本 payload 携带该文件的
    实时 diff 摘要（增删行数 + before/after 全文）。前端收到后：
    1. 立即把 diff 合并进本地变更集即时渲染，无需等待去抖全量刷新；
    2. 仍按原策略触发去抖全量校准，以最终一致为准。

    参数:
        task_id: 变更所属任务标识。
        turn_id: 产生该变更的轮次标识。
        path: 相对 workspace 的文件路径。
        action: 变更动作，取值 ``created`` / ``modified`` / ``deleted``。
        additions: 该文件实时新增行数（来自实际快照，非事件自身统计）。
        deletions: 该文件实时删除行数（来自实际快照，非事件自身统计）。
        before: 变更前文件全文；``created`` 时为 ``None``。
        after: 变更后文件全文；``deleted`` 时为 ``None``。
    """

    task_id: int
    turn_id: int
    path: str
    action: str
    additions: int = 0
    deletions: int = 0
    before: str | None = None
    after: str | None = None
