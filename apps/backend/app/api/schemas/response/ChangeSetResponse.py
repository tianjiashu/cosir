"""变更集响应模型。"""

from pydantic import BaseModel

from app.models.result.change_set import ChangeSet


class ChangeCheckpointResponse(BaseModel):
    """检查点响应项。"""

    run_id: int
    turn_seq: int
    label: str


class ChangeFileResponse(BaseModel):
    """文件变更响应项。"""

    path: str
    action: str
    status: str
    last_tool_call_id: str
    last_run_id: int
    additions: int
    deletions: int


class ChangeSetResponse(BaseModel):
    """某 task 的累积变更集响应。"""

    task_id: int
    checkpoints: list[ChangeCheckpointResponse]
    files: list[ChangeFileResponse]

    @classmethod
    def from_change_set(cls, change_set: ChangeSet) -> "ChangeSetResponse":
        """从服务层值对象构造响应模型。

        参数:
            change_set: 服务层返回的 ``ChangeSet``。

        返回:
            对应的 ``ChangeSetResponse``。

        异常:
            无。

        副作用:
            无。
        """
        return cls(
            task_id=change_set.task_id,
            checkpoints=[
                ChangeCheckpointResponse(run_id=c.run_id, turn_seq=c.turn_seq, label=c.label)
                for c in change_set.checkpoints
            ],
            files=[
                ChangeFileResponse(
                    path=f.path,
                    action=f.action,
                    status=f.status,
                    last_tool_call_id=f.last_tool_call_id,
                    last_run_id=f.last_run_id,
                    additions=f.additions,
                    deletions=f.deletions,
                )
                for f in change_set.files
            ],
        )
