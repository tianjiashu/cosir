"""``file_change_stable`` 事件 payload 值对象。"""

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class FileChangeStablePayload(RuntimeEventPayload):
    """某文件变更随所属 turn 结束而稳定，可展示与撤销。

    参数:
        task_id: 变更所属任务标识。
        turn_id: 产生该变更的轮次标识。
        path: 相对 workspace 的文件路径。
        action: 变更动作，取值 ``created`` / ``modified`` / ``deleted``。
    """

    task_id: str
    turn_id: str
    path: str
    action: str
